import requests

PORT = 8090

ROBOT_IP = "192.168.10.209"   # 현재 AP IP
SECRET = "19a11878aaab420fba94577ce3620dce"

TARGET_WIFI_SSID = "qweqweqwe"
TARGET_WIFI_PSK = "qweqweqwe1"
TARGET_ROUTE_MODE = "wlan0_first"


def switch_ap_to_wifi(robot_ip: str, secret: str):
    url = f"http://{robot_ip}:{PORT}/services/setup_wifi"

    payload = {
        "mode": "station",
        "ssid": TARGET_WIFI_SSID,
        "psk": TARGET_WIFI_PSK,
        "route_mode": TARGET_ROUTE_MODE,

        "advanced_params": {
            "ipv4.method": "manual",
            "ipv4.address": "192.168.10.210/24",
            "ipv4.gateway": "192.168.10.1",
            "ipv4.dns": "192.168.10.1"
        }
    }

    headers = {
        "Secret": secret,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=5)
        response.raise_for_status()
        print("WiFi 전환 요청 성공")
        print(response.text if response.text else "OK")

    except requests.RequestException as e:
        print("WiFi 전환 실패:", e)


if __name__ == "__main__":
    switch_ap_to_wifi(ROBOT_IP, SECRET)