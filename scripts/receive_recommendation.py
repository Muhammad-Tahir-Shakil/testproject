"""Receive and acknowledge one recommendation event from AWS SQS."""

import argparse
import json

import boto3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--wait-seconds", type=int, default=20)
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 20:
        raise SystemExit("--wait-seconds must be between 0 and 20.")

    sqs = boto3.client("sqs")
    response = sqs.receive_message(
        QueueUrl=args.queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=args.wait_seconds,
        MessageAttributeNames=["All"],
    )
    messages = response.get("Messages", [])
    if not messages:
        raise SystemExit("No recommendation event received before timeout.")

    message = messages[0]
    print(json.dumps(json.loads(message["Body"]), indent=2))
    sqs.delete_message(
        QueueUrl=args.queue_url,
        ReceiptHandle=message["ReceiptHandle"],
    )


if __name__ == "__main__":
    main()
