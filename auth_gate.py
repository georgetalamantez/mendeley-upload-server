import hashlib
import hmac
import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

COOKIE_NAME = "mendeley_uploader_auth_token"


def get_expected_password() -> str:
    return (os.getenv("APP_PASSWORD") or "").strip()


def is_auth_enabled() -> bool:
    return bool(get_expected_password())


def get_expected_token() -> str:
    pwd = get_expected_password().encode("utf-8")
    if not pwd:
        return ""
    return hmac.new(pwd, b"mendeley-uploader-authenticated-v1", hashlib.sha256).hexdigest()


def is_authenticated(request: Request) -> bool:
    if not is_auth_enabled():
        return True
    expected = get_expected_token()
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie_val) and hmac.compare_digest(cookie_val, expected)


def render_login_page(error_msg: str = "") -> str:
    error_html = (
        f'<div role="alert" aria-live="assertive" style="background:#ffebee;color:#b71c1c;padding:12px 16px;border-radius:8px;margin-bottom:16px;font-weight:500;">{error_msg}</div>'
        if error_msg
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign In - Mendeley PDF Uploader</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #f0f2f5;
            color: #333;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
            box-sizing: border-box;
        }}
        .login-card {{
            background: #ffffff;
            max-width: 420px;
            width: 100%;
            padding: 36px 32px;
            border-radius: 12px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.1);
            text-align: center;
        }}
        h1 {{
            margin: 0 0 8px 0;
            font-size: 1.7rem;
            color: #4a90e2;
        }}
        p {{
            color: #666;
            margin: 0 0 24px 0;
            font-size: 0.95rem;
        }}
        .form-group {{
            text-align: left;
            margin-bottom: 20px;
        }}
        label {{
            display: block;
            font-weight: 600;
            margin-bottom: 8px;
            font-size: 0.95rem;
        }}
        input[type="password"] {{
            width: 100%;
            padding: 12px 14px;
            font-size: 1rem;
            border: 1px solid #ddd;
            border-radius: 6px;
            box-sizing: border-box;
        }}
        input[type="password"]:focus {{
            outline: none;
            border-color: #4a90e2;
            box-shadow: 0 0 0 3px rgba(74, 144, 226, 0.2);
        }}
        button {{
            width: 100%;
            padding: 12px 18px;
            font-size: 1rem;
            font-weight: 600;
            color: #ffffff;
            background: #4a90e2;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            transition: background 0.2s;
        }}
        button:hover {{
            background: #357abd;
        }}
    </style>
</head>
<body>
    <main class="login-card">
        <h1>📚 Mendeley PDF Uploader</h1>
        <p>Please enter your password to access your Mendeley uploader.</p>
        {error_html}
        <form method="POST" action="login">
            <div class="form-group">
                <label for="password-input">Password</label>
                <input type="password" id="password-input" name="password" placeholder="Enter password" required autofocus autocomplete="current-password">
            </div>
            <button type="submit">Enter Mendeley Uploader</button>
        </form>
    </main>
</body>
</html>"""


class PasswordGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not is_auth_enabled():
            return await call_next(request)

        path = request.url.path
        if path in ("/login", "/logout", "/api/health"):
            return await call_next(request)

        if not is_authenticated(request):
            if request.method == "GET" and path in ("/", "/index.html", "/static/index.html"):
                return HTMLResponse(render_login_page(), status_code=200)
            return JSONResponse({"detail": "Authentication required"}, status_code=401)

        return await call_next(request)
