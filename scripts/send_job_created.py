"""Send a JobCreated fixture to the AWS SQS input queue."""

import argparse
import json
from pathlib import Path

import boto3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--file", default="data/job-created.json")
    args = parser.parse_args()

    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    boto3.client("sqs").send_message(
        QueueUrl=args.queue_url,
        MessageBody=json.dumps(payload),
        MessageAttributes={
            "event_type": {
                "DataType": "String",
                "StringValue": payload.get("event_type", "JobCreated"),
            }
        },
    )
    print(f"Sent {payload.get('event_id')} to the JobCreated queue.")


if __name__ == "__main__":
    main()
