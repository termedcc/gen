import tls_client
import base64
import random
import os

def load_proxies():
    if os.path.exists("proxies.txt"):
        with open("proxies.txt", "r") as f:
            return [line.strip() for line in f if line.strip()]
    return []

def get_account_data(email, password):
    proxies = load_proxies()
    proxy = random.choice(proxies) if proxies else None
    
    session = tls_client.Session(client_identifier="chrome_120")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    # 1. Login to Ubisoft
    b64_auth = base64.b64encode(f"{email}:{password}".encode('utf-8')).decode('utf-8')
    login_url = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
    headers = {
        "Content-Type": "application/json",
        "Ubi-AppId": "314d4fef-e568-454a-ae06-43e3bece12a6",
        "Authorization": f"Basic {b64_auth}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    
    try:
        login_res = session.post(login_url, headers=headers, json={"rememberMe": True})
        if login_res.status_code != 200:
            return {"error": "Invalid Ubisoft credentials or 2FA required."}

        data = login_res.json()
        ubi_token = data.get("ticket")
        ubi_session_id = data.get("sessionId")
        profile_id = data.get("profileId")
        username = "Unknown"

        # 2. Fetch Profile Info
        profile_url = f"https://public-ubiservices.ubi.com/v1/profiles/{profile_id}"
        profile_headers = {
            "Authorization": f"Ubi_v1 t={ubi_token}",
            "Ubi-SessionId": ubi_session_id,
            "Ubi-AppId": "314d4fef-e568-454a-ae06-43e3bece12a6",
            "User-Agent": "Mozilla/5.0"
        }
        
        profile_res = session.get(profile_url, headers=profile_headers)
        if profile_res.status_code == 200:
            username = profile_res.json().get("nameOnPlatform", "Unknown")

        # 3. Fetch Inventory
        space_id = "0d2ae42d-4c27-4cb7-af6c-2099062302bb" # R6 Siege
        inv_url = f"https://public-ubiservices.ubi.com/v1/profiles/{profile_id}/inventory?spaceId={space_id}"
        
        inv_res = session.get(inv_url, headers=profile_headers)
        
        if inv_res.status_code != 200:
            return {"error": "Failed to fetch inventory. (Maybe no R6 account linked?)"}

        inventory_data = inv_res.json()
        
        return {
            "success": True,
            "profile_id": profile_id,
            "username": username,
            "inventory": inventory_data
        }
    except Exception as e:
        return {"error": f"Server error: {str(e)}"}
