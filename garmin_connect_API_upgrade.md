You are spot on—you aren't doing anything wrong. Garmin significantly ramped up its anti-bot protections via Cloudflare in **late March 2026**, specifically targeting programmatic logins from common Python libraries.

The "broken" state you’re seeing is due to **TLS Fingerprinting**. Cloudflare now checks the underlying SSL/TLS handshake patterns; since standard Python libraries (like `requests` or `urllib`) have distinct fingerprints compared to a real web browser (Chrome/Safari), the authentication request is blocked before it even hits Garmin’s SSO login page.

---

### The Current Situation (April 2026)

- **The "Garth" Library:** This was the go-to authentication backend for most Garmin tools. It was officially deprecated around **March 28, 2026**, because it could no longer bypass the new SSO flow and Cloudflare's bot detection.
- **python-garminconnect:** If you are using this library, versions prior to **0.3.0** are currently broken.
- **New Requirements:** To get past Cloudflare now, a client must impersonate a real browser's TLS stack.

---

### How to Fix It

The community has already scrambled to release a fix. You need to upgrade your environment to use **native TLS impersonation**.

#### 1. Update your library

The main `python-garminconnect` library was updated (v0.3.0+) to include a native authentication engine that uses `curl_cffi` to mimic browser fingerprints.

Bash

```
pip install --upgrade python-garminconnect
```

#### 2. Install the necessary "Impersonation" dependencies

The new login flow requires libraries that can handle browser-like headers and TLS signatures:

Bash

```
pip install curl_cffi ua-generator
```

#### 3. Fresh Authentication

Because the token format has changed to accommodate the new "DI OAuth" flow, your old session files (if any) are invalid.

- Delete any existing `~/.garminconnect` folder or local token files.
- Run your script again to trigger a fresh login. It should now use `curl_cffi` to "pretend" to be a Chrome or Safari browser, which usually clears the Cloudflare hurdle.

### Summary Table of Changes

| **Feature**       | **Old Method (Broken)**   | **New Method (Working)**                   |
|---------------|-----------------------|----------------------------------------|
| **HTTP Client**   | `requests`              | `curl_cffi` (impersonates Chrome/Safari) |
| **Auth Library**  | `garth`                 | Native DI OAuth engine                 |
| **Token Storage** | `token.json`            | `garmin_tokens.json`                     |
| **Detection**     | Easily flagged as bot | Randomized browser fingerprinting      |

If you are still getting blocked, Garmin may have flagged your IP address temporarily due to the previous failed attempts. Try running your script from a different network (or a VPN) once you've updated the code.