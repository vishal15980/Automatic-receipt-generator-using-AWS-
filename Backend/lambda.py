
import json
import os
import uuid
import html
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
ses = boto3.client("ses")

TABLE_NAME = os.environ.get(
    "DYNAMO_TABLE",
    os.environ.get("DYNAMODB_TABLE", "Receipts")
)
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_RECEIPT_PREFIX", "generated/")
SENDER_EMAIL = os.environ.get("SES_SENDER_EMAIL", "")
ALLOWED_ORIGIN = os.environ.get(
    "ALLOWED_ORIGIN",
    "https://d11eajog8p3vb7.cloudfront.net"
)

# Prices are controlled by the backend, not the browser.
PRODUCTS = {
    "soil-sensor-kit": {
        "name": "Soil Sensor Kit",
        "price": Decimal("1499.00")
    },
    "smart-plant-monitor": {
        "name": "Smart Plant Monitor",
        "price": Decimal("999.00")
    },
    "garden-tool-kit": {
        "name": "Garden Tool Kit",
        "price": Decimal("699.00")
    }
}

table = dynamodb.Table(TABLE_NAME)


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS"
        },
        "body": json.dumps(body, default=str)
    }


def get_method(event):
    return (
        event.get("requestContext", {})
        .get("http", {})
        .get("method", event.get("httpMethod", ""))
        .upper()
    )


def get_path(event):
    return (
        event.get("rawPath")
        or event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path", "")
    ).rstrip("/")


def lambda_handler(event, context):
    print("Received event:", json.dumps(event, default=str))

    method = get_method(event)
    path = get_path(event)

    if method == "OPTIONS":
        return response(200, {"message": "CORS preflight successful"})

    if method == "POST" and path.endswith("/purchase"):
        return create_purchase(event)

    if method == "GET" and path.endswith("/receipts"):
        return get_receipts(event)

    return response(404, {
        "error": "Route not supported",
        "method": method,
        "path": path
    })


def create_purchase(event):
    try:
        raw_body = event.get("body") or "{}"

        if isinstance(raw_body, str):
            body = json.loads(raw_body)
        else:
            body = raw_body

        email = str(body.get("email", "")).strip().lower()
        product_id = str(body.get("productId", "")).strip()
        quantity = body.get("quantity")

        if (
            len(email) > 254
            or "@" not in email
            or email.startswith("@")
            or email.endswith("@")
        ):
            return response(400, {"error": "Enter a valid email address"})

        if isinstance(quantity, bool) or not isinstance(quantity, int):
            return response(400, {"error": "Quantity must be a whole number"})

        if quantity < 1 or quantity > 10:
            return response(400, {"error": "Quantity must be between 1 and 10"})

        product = PRODUCTS.get(product_id)
        if not product:
            return response(400, {"error": "Invalid product selected"})

        if not SENDER_EMAIL:
            return response(500, {"error": "SES_SENDER_EMAIL is not configured"})

        if not S3_BUCKET:
            return response(500, {"error": "S3_BUCKET is not configured"})

        receipt_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        receipt_date = now.isoformat(timespec="seconds")

        unit_price = product["price"]
        total = (unit_price * quantity).quantize(Decimal("0.01"))

        receipt_text = (
            "PURCHASE RECEIPT\n"
            "------------------------------\n"
            f"Receipt ID: {receipt_id}\n"
            f"Date: {receipt_date}\n"
            f"Customer email: {email}\n"
            f"Product: {product['name']}\n"
            f"Quantity: {quantity}\n"
            f"Unit price: INR {unit_price:.2f}\n"
            f"Total: INR {total:.2f}\n"
            "\nDemo order only. No payment was processed.\n"
        )

        s3_key = f"{S3_PREFIX.rstrip('/')}/{receipt_id}.txt"

        # Save the receipt file to S3.
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=receipt_text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8"
        )

        item = {
            "receipt_id": receipt_id,
            "date": receipt_date,
            "email": email,
            "product_id": product_id,
            "product_name": product["name"],
            "quantity": quantity,
            "unit_price": unit_price,
            "total": total,
            "s3_key": s3_key,
            "status": "DEMO_ORDER"
        }

        # Save the receipt record using your existing table keys.
        table.put_item(Item=item)

        safe_name = html.escape(product["name"])
        safe_email = html.escape(email)
        safe_id = html.escape(receipt_id)

        email_html = f"""
        <html>
          <body>
            <h2>Purchase receipt</h2>
            <p>Receipt ID: {safe_id}</p>
            <p>Date: {html.escape(receipt_date)}</p>
            <p>Customer: {safe_email}</p>
            <p>Product: {safe_name}</p>
            <p>Quantity: {quantity}</p>
            <p>Unit price: INR {unit_price:.2f}</p>
            <h3>Total: INR {total:.2f}</h3>
            <p>This is a demo order. No payment was processed.</p>
          </body>
        </html>
        """

        # Send email to the address supplied by the customer.
        try:
            email_result = ses.send_email(
                Source=SENDER_EMAIL,
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {
                        "Data": f"Your receipt - {receipt_id}"
                    },
                    "Body": {
                        "Text": {"Data": receipt_text},
                        "Html": {"Data": email_html}
                    }
                }
            )
        except Exception as email_error:
            print("SES send failed:", str(email_error))
            return response(502, {
                "message": "Order and receipt were saved, but email delivery failed",
                "receipt_id": receipt_id,
                "email_error": str(email_error)
            })

        return response(201, {
            "message": "Demo order created and receipt email submitted",
            "receipt_id": receipt_id,
            "date": receipt_date,
            "email": email,
            "product": product["name"],
            "quantity": quantity,
            "unit_price": str(unit_price),
            "total": str(total),
            "currency": "INR",
            "s3_key": s3_key,
            "email_message_id": email_result["MessageId"],
            "payment_processed": False
        })

    except Exception as error:
        print("Purchase error:", str(error))
        return response(500, {"error": "Unable to create purchase"})


def get_receipts(event):
    try:
        params = event.get("queryStringParameters") or {}
        email = str(params.get("email", "")).strip().lower()

        # Without an email, return a limited recent scan for demonstration.
        if email:
            result = table.query(
                IndexName="customer-email-date-index",
                KeyConditionExpression=Key("email").eq(email),
                ScanIndexForward=False,
                Limit=50
            )
        else:
            result = table.scan(Limit=50)

        items = result.get("Items", [])

        return response(200, {
            "message": "Receipts retrieved successfully",
            "count": len(items),
            "receipts": items
        })

    except Exception as error:
        print("Get receipts error:", str(error))
        return response(500, {"error": "Unable to retrieve receipts"})
