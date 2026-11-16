"""VietQR image links; only the seller confirms receipt of money."""
from dataclasses import dataclass
import os
import re
from urllib.parse import urlencode


@dataclass(frozen=True)
class BankDetails:
    bank: str
    account: str
    name: str

    @classmethod
    def from_env(cls):
        value = cls(os.getenv("BANK_BIN", "").strip(), os.getenv("BANK_ACCOUNT", "").strip(),
                    os.getenv("BANK_ACCOUNT_NAME", "").strip())
        if not re.fullmatch(r"[0-9]{6}", value.bank):
            raise ValueError("Điền BANK_BIN gồm 6 chữ số của ngân hàng.")
        if not re.fullmatch(r"[A-Za-z0-9]{1,19}", value.account) or not 1 <= len(value.name) <= 100:
            raise ValueError("Điền BANK_ACCOUNT (1–19 chữ/số) và BANK_ACCOUNT_NAME.")
        return value

    def qr_url(self, amount, memo):
        if type(amount) is not int or amount <= 0 or not re.fullmatch(r"[A-Z0-9]{6}", memo):
            raise ValueError("Số tiền hoặc mã chuyển khoản không hợp lệ.")
        return f"https://img.vietqr.io/image/{self.bank}-{self.account}-compact2.png?" + urlencode(
            {"amount": amount, "addInfo": memo, "accountName": self.name})
