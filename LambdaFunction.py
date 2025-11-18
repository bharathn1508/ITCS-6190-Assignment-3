import os
import csv
import io
import json
import boto3
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Tuple

s3 = boto3.client("s3")

# -------- Config via environment variables ----------
RAW_PREFIX        = os.getenv("RAW_PREFIX", "raw/").strip("/")
PROCESSED_PREFIX  = os.getenv("PROCESSED_PREFIX", "processed/").strip("/")
DAYS_WINDOW       = int(os.getenv("DAYS_WINDOW", "30"))   # keep orders newer than N days if pending/cancelled
OUTPUT_PREFIX_TAG = os.getenv("OUTPUT_PREFIX_TAG", "")    # e.g., "filtered_" to prefix output filename
# ----------------------------------------------------

# Accept common date formats; add more if needed
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"]

def parse_date(value: str) -> datetime:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            continue
    raise ValueError(f"Unrecognized date format: {value!r}")

def normalize_headers(headers: List[str]) -> Tuple[dict, List[str]]:
    """
    Build a normalization map so we can find columns regardless of case/spacing/underscores.
    Returns (norm_map, original_headers).
    norm_map: normalized_key -> original_header
    """
    headers = headers or []
    norm_map = {}
    for h in headers:
        norm = (h or "").strip().lower().replace(" ", "").replace("_", "")
        if norm and norm not in norm_map:
            norm_map[norm] = h
    return norm_map, headers

def select_column(norm_map: dict, candidates: List[str]) -> str:
    """Pick the first matching header from a list of normalized candidates."""
    for cand in candidates:
        key = cand.strip().lower().replace("_", "")
        if key in norm_map:
            return norm_map[key]
    return ""

def lambda_handler(event, context):
    print("Lambda triggered.")
    # ---- Figure out which object was uploaded ----
    try:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"], encoding="utf-8")
    except Exception as e:
        print(f"Bad event structure: {json.dumps(event)[:500]}")
        raise

    print(f"Incoming: s3://{bucket}/{key}")

    # Only process keys under RAW_PREFIX/
    if not key.startswith(RAW_PREFIX + "/") and not key.startswith(RAW_PREFIX):
        print(f"Key not under '{RAW_PREFIX}/'; skipping.")
        return {"statusCode": 200, "body": "Not a raw/ object, ignored."}

    # ---- Read the CSV from S3 ----
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read().decode("utf-8").splitlines()
    except Exception as e:
        print(f"Error reading s3://{bucket}/{key}: {e}")
        raise

    if not body:
        msg = "Empty file; nothing to process."
        print(msg)
        return {"statusCode": 200, "body": msg}

    reader = csv.DictReader(body)
    norm_map, original_headers = normalize_headers(reader.fieldnames)
    if not original_headers:
        raise ValueError("CSV has no header row.")

    # Identify required columns (flexible names)
    status_col = select_column(norm_map, ["status", "orderstatus"])
    date_col   = select_column(norm_map, ["orderdate", "date", "order_date"])
    if not status_col or not date_col:
        raise KeyError(f"Missing required columns. Found headers={original_headers}")

    print(f"Detected columns -> status='{status_col}', date='{date_col}'")

    # ---- Filter logic ----
    kept_rows = []
    total = 0
    bad_date = 0
    filtered_out = 0
    cutoff = datetime.now() - timedelta(days=DAYS_WINDOW)

    for row in reader:
        total += 1
        status = (row.get(status_col) or "").strip().lower()
        date_raw = (row.get(date_col) or "").strip()

        try:
            order_dt = parse_date(date_raw)
        except Exception:
            bad_date += 1
            # Skip rows with non-parsable date
            continue

        # Keep unless it's (pending/cancelled AND older than cutoff)
        if status not in {"pending", "cancelled"} or order_dt > cutoff:
            kept_rows.append(row)
        else:
            filtered_out += 1

    print(f"Processed={total}, Kept={len(kept_rows)}, Filtered={filtered_out}, BadDate={bad_date}")

    # ---- Write output CSV to processed/ ----
    out_buf = io.StringIO()
    writer = csv.DictWriter(out_buf, fieldnames=original_headers)
    writer.writeheader()
    if kept_rows:
        writer.writerows(kept_rows)

    filename = key.split("/")[-1]
    if OUTPUT_PREFIX_TAG:
        out_name = OUTPUT_PREFIX_TAG + filename
        processed_key = f"{PROCESSED_PREFIX}/{out_name}"
    else:
        # mirror path; replace first 'raw/' with 'processed/'
        processed_key = key.replace(RAW_PREFIX, PROCESSED_PREFIX, 1)

    try:
        s3.put_object(
            Bucket=bucket,
            Key=processed_key,
            Body=out_buf.getvalue().encode("utf-8"),
            ContentType="text/csv; charset=utf-8",
        )
    except Exception as e:
        print(f"Error writing s3://{bucket}/{processed_key}: {e}")
        raise

    print(f"Wrote {len(kept_rows)} rows -> s3://{bucket}/{processed_key}")
    return {
        "statusCode": 200,
        "body": json.dumps({
            "bucket": bucket,
            "input_key": key,
            "output_key": processed_key,
            "processed": len(kept_rows),
            "skipped_bad_date": bad_date,
            "filtered_out": filtered_out
        })
    }
