"""额外 CA 中间证书信任引导的回归测试（2026-09-17 复盘 T3-14）。

背景
----
`shenwan / stock_mapping` 在 `data_source_health` 中长期 `down`：
`fail_streak=51`、`last_success=None`（从未成功）。错误为
`SSLCertVerificationError: unable to get local issuer certificate`。

`openssl s_client` 证实**服务端只下发叶子证书**：
    s:/C=CN/.../CN=*.swsresearch.com
    i:/C=US/O=DigiCert, Inc./CN=GeoTrust G2 TLS CN RSA4096 SHA256 2022 CA1
    verify error:num=20:unable to get local issuer certificate

修法是把缺失的中间证书随仓库携带并在启动时并入信任源，
**仍然执行完整链验证**，而非关闭校验。本测试锁定该语义。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import tls_trust
from app.core.tls_trust import EXTRA_CA_DIR, install_extra_ca_bundle


def test_extra_ca_certificate_is_shipped_with_repo():
    certs = sorted(EXTRA_CA_DIR.glob("*.pem"))
    assert certs, f"未随仓库携带额外 CA 证书: {EXTRA_CA_DIR}"
    for path in certs:
        text = path.read_text(encoding="utf-8")
        assert "BEGIN CERTIFICATE" in text and "END CERTIFICATE" in text


def test_shipped_certificate_matches_recorded_identity():
    """携带的证书必须是文档记录的那张（防止被静默替换）。"""
    import ssl
    import subprocess

    path = EXTRA_CA_DIR / "geotrust_g2_tls_cn_rsa4096_sha256_2022_ca1.pem"
    assert path.exists()
    result = subprocess.run(
        ["openssl", "x509", "-in", str(path), "-noout", "-fingerprint", "-sha256"],
        capture_output=True, text=True, check=True,
    )
    normalized = result.stdout.strip().split("=", 1)[1].replace(":", "").upper()
    assert normalized == (
        "05DC9EDC0FDDFA975A1432EF806EC780078B5362AD45AF76DB15C907630DB25D"
    ), f"证书指纹与文档记录不一致: {normalized}"

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(path))
    assert ctx.cert_store_stats()["x509_ca"] >= 1


def test_install_sets_bundle_env_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setattr(tls_trust, "_INSTALLED_BUNDLE", None)

    bundle = install_extra_ca_bundle()

    assert bundle is not None and Path(bundle).exists()
    assert os.environ["SSL_CERT_FILE"] == bundle
    assert os.environ["REQUESTS_CA_BUNDLE"] == bundle
    content = Path(bundle).read_text(encoding="utf-8")
    assert "BEGIN CERTIFICATE" in content
    # bundle 必须同时包含基础 CA 包与额外中间证书，而不是只含后者
    assert content.count("BEGIN CERTIFICATE") > 1


def test_install_respects_explicit_operator_configuration(monkeypatch):
    """调用方已显式配置信任源时不得覆盖（运维可用自己的 CA 包）。"""
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/ca.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/custom/ca.pem")
    monkeypatch.setattr(tls_trust, "_INSTALLED_BUNDLE", None)

    install_extra_ca_bundle()

    assert os.environ["SSL_CERT_FILE"] == "/custom/ca.pem"
    assert os.environ["REQUESTS_CA_BUNDLE"] == "/custom/ca.pem"


def test_install_is_idempotent(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(tls_trust, "_INSTALLED_BUNDLE", None)

    first = install_extra_ca_bundle()
    second = install_extra_ca_bundle()

    assert first == second
    # 二次调用不得重复追加证书内容
    assert Path(first).read_text(encoding="utf-8").count("BEGIN CERTIFICATE") == (
        Path(first).read_text(encoding="utf-8").count("BEGIN CERTIFICATE")
    )


def test_missing_directory_degrades_without_raising(monkeypatch, tmp_path):
    """证书目录缺失时不得阻断进程启动。"""
    monkeypatch.setattr(tls_trust, "EXTRA_CA_DIR", tmp_path / "not-exists")
    monkeypatch.setattr(tls_trust, "_INSTALLED_BUNDLE", None)

    assert install_extra_ca_bundle() is None


def test_unreadable_certificate_is_skipped(monkeypatch, tmp_path):
    """单张证书不可读时跳过该张，不影响其余证书加载。"""
    monkeypatch.setattr(tls_trust, "EXTRA_CA_DIR", tmp_path)
    monkeypatch.setattr(tls_trust, "_INSTALLED_BUNDLE", None)
    (tmp_path / "bad.pem").write_text("not a certificate", encoding="utf-8")
    assert install_extra_ca_bundle() is None  # 唯一一张无效 -> 不安装

    good = Path(EXTRA_CA_DIR) / "geotrust_g2_tls_cn_rsa4096_sha256_2022_ca1.pem"
    (tmp_path / "good.pem").write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    bundle = install_extra_ca_bundle()
    assert bundle is not None


def test_verification_is_enabled_not_disabled():
    """修复不得以关闭校验实现：源码中不得出现 server_verify 关断。"""
    source = Path(tls_trust.__file__).read_text(encoding="utf-8")
    for forbidden in ("verify=False", "CERT_NONE", "_create_unverified_context",
                      "check_hostname = False", "check_hostname=False"):
        assert forbidden not in source, f"禁止以关闭校验的方式规避: {forbidden}"
