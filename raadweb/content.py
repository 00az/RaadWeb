"""The approved public facts the deployed pages must show, checked by the smoke test.

The two addresses are the public contacts the person approved for "Request test access" at plan
approval. They are not the operational alert address, which is never written in this repo.
"""

from __future__ import annotations

PUBLIC_CONTACTS = ("abdulaziz.alrubai@outlook.com", "zyad.alamri777@gmail.com")
PRODUCT_NAME = "RAAD"
LOGO_ALT = "رعد RAAD"
DESCRIPTIONS = {
    "ar": "رعد محفظة تجريبية لإرسال مبالغ افتراضية بالريال السعودي بين مستخدمي رعد.",
    "en": "RAAD is a demo wallet for sending simulated Saudi riyals between RAAD users.",
}
