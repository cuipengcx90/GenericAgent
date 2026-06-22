"""
场景2：邮件自动处理
轮询 IMAP 收件箱，有未读邮件 → Agent 自动分类并起草回复
启动：python agentmain.py --reflect reflect_scripts/02_email_handler.py
"""
import sys, imaplib, email
from email.header import decode_header
sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu

INTERVAL = 120  # 每2分钟检查一次

# ===== 配置区 =====
IMAP_HOST   = "imap.gmail.com"   # 邮件服务器，Gmail: imap.gmail.com, QQ: imap.qq.com
IMAP_PORT   = 993
EMAIL_ADDR  = "your@email.com"   # 你的邮箱
EMAIL_PASS  = "your_app_password" # 应用专用密码（非登录密码）
# ==================

_processed = set()

def _decode_str(s):
    parts = decode_header(s or "")
    result = []
    for b, enc in parts:
        if isinstance(b, bytes):
            result.append(b.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(b)
    return "".join(result)

def check():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(EMAIL_ADDR, EMAIL_PASS)
        mail.select("INBOX")
        _, ids = mail.search(None, "UNSEEN")
        new_ids = [i for i in ids[0].split() if i not in _processed]
        if not new_ids:
            mail.logout()
            return None
        # 取最新一封
        uid = new_ids[-1]
        _, data = mail.fetch(uid, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])
        subject = _decode_str(msg.get("Subject", "（无主题）"))
        sender  = _decode_str(msg.get("From", ""))
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")[:1000]
                    break
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")[:1000]
        _processed.add(uid)
        mail.logout()
        return (f"收到新邮件，请帮我处理：\n"
                f"发件人：{sender}\n主题：{subject}\n内容：{body}\n\n"
                f"请：1) 判断邮件类型（工作/推广/通知等）2) 起草一封回复（若需要）3) 给出处理建议")
    except Exception as e:
        print(f"[邮件检查失败] {e}")
        return None

def on_done(result: str):
    notify_feishu(f"📧 邮件助手\n\n{result}")