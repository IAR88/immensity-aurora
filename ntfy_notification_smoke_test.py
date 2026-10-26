import os
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen


MESSAGE = "IMMENSITY AURORA: Ntfy integration successful."


def main():
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError("Set NTFY_TOPIC before sending a Ntfy test notification.")
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Content-Type": "text/plain; charset=utf-8", "Title": "Ntfy test"}
    access_token = os.getenv("NTFY_ACCESS_TOKEN", "").strip()
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = Request(
        f"{server}/{quote(topic, safe='-_.')}",
        data=MESSAGE.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            print(f"HTTP response status code: {response.status}")
    except HTTPError as error:
        print(f"HTTP response status code: {error.code}")
        raise


if __name__ == "__main__":
    main()
