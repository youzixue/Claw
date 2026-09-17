"""额外 CA 中间证书的进程级信任引导（2026-09-17 复盘 T3-14）。

问题
----
`data_source_health` 中 `shenwan / stock_mapping` 状态长期 `down`：
`fail_streak=51`、`last_success=None`（**从未成功过一次**），错误为::

    SSLError(... https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls
      Caused by SSLError(SSLCertVerificationError(1,
      '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
       unable to get local issuer certificate (_ssl.c:1006)')))

该接口由 akshare 的 `stock_industry_clf_hist_sw` 提供。用 `openssl s_client`
检查可知**服务端只下发叶子证书、不下发中间证书**：:

    0 s:/C=CN/.../CN=*.swsresearch.com
      i:/C=US/O=DigiCert, Inc./CN=GeoTrust G2 TLS CN RSA4096 SHA256 2022 CA1
    verify error:num=20:unable to get local issuer certificate

即这是服务端证书链配置缺陷，**不是本机缺少 CA 包**（`certifi` 已安装；仅用
certifi 仍然失败，已验证）。

修法
----
把 CA 官方仓库发布的缺失中间证书随仓库携带，并在进程启动时把它并入一个
临时 CA bundle，通过 `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` 交给 OpenSSL
与 requests。

**这不是关闭校验**：仍然执行完整的链验证（叶子 → 中间 → DigiCert Global Root G2），
只是把服务端漏发的中间证书补齐。已实测补链后 TLS 握手通过（TLSv1.3）。

来源与凭据
----------
下载自 DigiCert 官方 CA 仓库：
https://cacerts.digicert.com/GeoTrustG2TLSCNRSA4096SHA2562022CA1.crt
subject = /C=US/O=DigiCert, Inc./CN=GeoTrust G2 TLS CN RSA4096 SHA256 2022 CA1
issuer  = /C=US/O=DigiCert Inc/OU=www.digicert.com/CN=DigiCert Global Root G2
valid   = 2022-12-15 .. 2032-12-14
SHA256  = 05:DC:9E:DC:0F:DD:FA:97:5A:14:32:EF:80:6E:C7:80:07:8B:53:62:AD:45:AF:76:DB:15:C9:07:63:0D:B2:5D
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from loguru import logger

# 随仓库携带的额外中间证书（PEM）。新增时在此登记即可，不必改调用方。
EXTRA_CA_DIR = Path(__file__).resolve().parent.parent / "data" / "certs"

_INSTALLED_BUNDLE: str | None = None


def _read_extra_certs() -> str:
    if not EXTRA_CA_DIR.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(EXTRA_CA_DIR.glob("*.pem")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:  # 单张证书不可读不得阻断进程启动
            logger.warning(f"额外 CA 证书不可读，已跳过: {path.name} ({exc})")
            continue
        if "BEGIN CERTIFICATE" in text:
            parts.append(text.strip())
    return "\n".join(parts)


def install_extra_ca_bundle() -> str | None:
    """构建 certifi + 额外中间证书的 bundle，并在未显式设置时接管进程信任源。

    返回 bundle 路径；无法构建时返回 None（不影响启动）。
    """
    global _INSTALLED_BUNDLE
    if _INSTALLED_BUNDLE is not None:
        return _INSTALLED_BUNDLE

    extra = _read_extra_certs()
    if not extra:
        return None

    try:
        import certifi

        base = Path(certifi.where()).read_text(encoding="utf-8")
    except Exception as exc:  # certifi 缺失时退回额外证书自身
        logger.warning(f"certifi 证书包不可用，仅使用额外 CA: {exc}")
        base = ""

    bundle_path = Path(tempfile.gettempdir()) / "claw_ca_bundle.pem"
    try:
        bundle_path.write_text(f"{base}\n{extra}\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(f"CA bundle 写入失败，跳过额外信任引导: {exc}")
        return None

    # 尊重调用方已显式配置的信任源，不覆盖。
    os.environ.setdefault("SSL_CERT_FILE", str(bundle_path))
    os.environ.setdefault("REQUESTS_CA_BUNDLE", str(bundle_path))
    _INSTALLED_BUNDLE = str(bundle_path)
    logger.info(
        f"已加载额外 CA 中间证书 {len(list(EXTRA_CA_DIR.glob('*.pem')))} 张，"
        f"bundle={bundle_path}"
    )
    return _INSTALLED_BUNDLE
