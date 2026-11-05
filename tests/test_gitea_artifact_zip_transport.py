"""Negative transport checks for Gitea Actions private ZIPs, without live credentials."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_artifacts_transport import (
    ArtifactTransportError,
    require_patched_gitea_version,
    stream_action_artifact_zip,
    validate_zip_archive,
)

PAT = "TEST-NEVER-FORWARD-GITEA-PAT"
SOURCE = "https://gitea.internal/api/v1"
BLOB = "https://artifacts.example.test:8443/archive/signed?sig=SENSITIVE-NEVER-LOG"


def make_zip(name: str = "index.html", content: bytes = b"<html>safe</html>") -> bytes:
    file = io.BytesIO()
    with zipfile.ZipFile(file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return file.getvalue()


def client(handler, *, blob: bool = False, version=None):
    headers = {} if blob else {"Authorization": f"token {PAT}"}

    def with_version(req):
        if req.url.path.endswith("/version"):
            return httpx.Response(
                200, json={"version": "1.26.2"} if version is None else version
            )
        return handler(req)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler if blob else with_version),
        base_url="https://artifacts.example.test:8443" if blob else SOURCE,
        headers=headers,
        follow_redirects=False,
    )


async def run(api_handler, blob_handler=None, *, cap=20 * 1024 * 1024, checksum=None, origins=None):
    result = io.BytesIO()
    async with client(api_handler) as api:
        if blob_handler is not None:
            async with client(blob_handler, blob=True) as blob:
                output = await stream_action_artifact_zip(
                    api, "gpakoh", "xloud", 17, result,
                    redirect_origins=["https://artifacts.example.test:8443"] if origins is None else origins,
                    max_bytes=cap, blob_client=blob, expected_sha256=checksum,
                )
        else:
            output = await stream_action_artifact_zip(
                api, "gpakoh", "xloud", 17, result,
                redirect_origins=[] if origins is None else origins,
                max_bytes=cap, expected_sha256=checksum,
            )
    return output, result.getvalue()


@pytest.mark.asyncio
async def test_signed_redirect_does_not_forward_pat_or_url_in_return_value():
    blob_bytes = make_zip()
    recorded = []

    def api(req):
        recorded.append(("api", dict(req.headers), str(req.url)))
        assert req.url.path.endswith("/repos/gpakoh/xloud/actions/artifacts/17/zip")
        assert req.headers["authorization"] == f"token {PAT}"
        return httpx.Response(302, headers={"Location": BLOB})

    def blob(req):
        recorded.append(("blob", dict(req.headers), str(req.url)))
        assert "authorization" not in req.headers
        assert req.url.query == b"sig=SENSITIVE-NEVER-LOG"
        return httpx.Response(200, content=blob_bytes)

    result, received = await run(api, blob)
    assert len(recorded) == 2
    assert result == {
        "size_bytes": len(blob_bytes),
        "sha256": hashlib.sha256(blob_bytes).hexdigest(),
        "entries": 1,
        "uncompressed_bytes": len(b"<html>safe</html>"),
    }
    assert received == blob_bytes
    assert PAT not in str(result)
    assert "SENSITIVE-NEVER-LOG" not in str(result)


@pytest.mark.asyncio
async def test_direct_200_from_gitea_is_bounded_and_validated():
    data = make_zip()
    output, received = await run(lambda req: httpx.Response(200, content=data))
    assert output["size_bytes"] == len(data)
    assert received == data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.test/archive?token=SECRET",
        "http://artifacts.example.test:8443/archive",
        "https://user:pass@artifacts.example.test:8443/archive",
        "https://artifacts.example.test:8443\\@evil.example.test/",
        "https://artifacts.example.test:8443/archive\r\nAuthorization: token SECRET",
        "https://artifacts.example.test:8443/archive#fragment",
        "",
    ],
)
async def test_unapproved_redirects_fail_before_blob_network_request(location):
    calls = []

    def api(req):
        return httpx.Response(302, headers={"Location": location})

    def blob(req):
        calls.append(req)
        return httpx.Response(200, content=make_zip())

    with pytest.raises(ArtifactTransportError):
        await run(api, blob)
    assert calls == []


@pytest.mark.asyncio
async def test_second_redirect_is_not_followed_even_with_approved_origin():
    calls = []

    def api(req):
        return httpx.Response(302, headers={"Location": BLOB})

    def blob(req):
        calls.append(req)
        return httpx.Response(302, headers={"Location": BLOB})

    with pytest.raises(ArtifactTransportError, match="did not return ZIP"):
        await run(api, blob)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_too_large_download_truncates_temp_file_on_failure():
    data = make_zip(content=b"large" * 200)
    file = io.BytesIO()
    async with client(lambda req: httpx.Response(200, content=data)) as api:
        with pytest.raises(ArtifactTransportError, match="byte cap"):
            await stream_action_artifact_zip(
                api, "gpakoh", "xloud", 17, file,
                redirect_origins=[], max_bytes=60,
            )
    assert file.getvalue() == b""


@pytest.mark.asyncio
async def test_digest_mismatch_is_failed_closed_and_erases_download():
    data = make_zip()
    file = io.BytesIO()
    async with client(lambda req: httpx.Response(200, content=data)) as api:
        with pytest.raises(ArtifactTransportError, match="checksum"):
            await stream_action_artifact_zip(
                api, "gpakoh", "xloud", 17, file,
                redirect_origins=[], expected_sha256="0" * 64,
            )
    assert file.getvalue() == b""


@pytest.mark.asyncio
async def test_invalid_zip_is_erased_even_when_bytes_fit_cap():
    file = io.BytesIO()
    async with client(lambda req: httpx.Response(200, content=b"not-zip")) as api:
        with pytest.raises(ArtifactTransportError, match="valid bounded ZIP"):
            await stream_action_artifact_zip(
                api, "gpakoh", "xloud", 17, file, redirect_origins=[],
            )
    assert file.getvalue() == b""


@pytest.mark.parametrize("filename", ["../secret", "/absolute", "dir/../../secret", "C:\\private", "dir\\file"])
def test_zip_member_traversal_rejected(filename):
    with pytest.raises(ArtifactTransportError, match="unsafe member"):
        validate_zip_archive(io.BytesIO(make_zip(filename)))


def test_symlink_member_rejected_without_extraction():
    file = io.BytesIO()
    with zipfile.ZipFile(file, "w") as archive:
        member = zipfile.ZipInfo("linked-private")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, "/etc/passwd")
    with pytest.raises(ArtifactTransportError, match="unsafe member"):
        validate_zip_archive(file)


def test_zip_bomb_rejected_without_decompression():
    with pytest.raises(ArtifactTransportError, match="compression ratio"):
        validate_zip_archive(io.BytesIO(make_zip(content=b"0" * (1024 * 1024))))


@pytest.mark.parametrize("server", [
    {"version": "1.26.1"}, {"version": "1.25.9"}, {"version": "1.26.2-rc1"},
    {"version": "1.26"}, {"version": ""}, {"version": "1.99.0\nSECRET"},
    None, {"version": True},
])
def test_unproven_or_vulnerable_server_is_rejected(server):
    with pytest.raises(ArtifactTransportError):
        require_patched_gitea_version(server)


@pytest.mark.parametrize("server", [{"version": "1.26.2"}, {"version": "1.27.0"}, {"version": "1.26.4+gitea"}])
def test_patched_server_version_is_accepted(server):
    require_patched_gitea_version(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("id_", [0, True, "17", -1])
async def test_invalid_artifact_id_fails_before_network(id_):
    calls = []
    async with client(lambda req: (calls.append(req), httpx.Response(200))[1]) as api:
        with pytest.raises(ArtifactTransportError):
            await stream_action_artifact_zip(
                api, "gpakoh", "xloud", id_, io.BytesIO(), redirect_origins=[],
            )
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("origins", [
    ["http://artifacts.example.test"], ["https://evil.example.test/archive"],
    ["https://user@artifacts.example.test"], ["https://artifacts.example.test/?q=1"],
])
async def test_invalid_operator_allowlist_fails_before_network(origins):
    calls = []
    async with client(lambda req: (calls.append(req), httpx.Response(200))[1]) as api:
        with pytest.raises(ArtifactTransportError):
            await stream_action_artifact_zip(
                api, "gpakoh", "xloud", 17, io.BytesIO(), redirect_origins=origins,
            )
    assert calls == []

@pytest.mark.asyncio
async def test_vulnerable_server_refuses_before_issuing_signed_url():
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/version"):
            return httpx.Response(200, json={"version": "1.26.1"})
        return httpx.Response(200, content=make_zip())

    file = io.BytesIO()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url=SOURCE,
        headers={"Authorization": f"token {PAT}"},
    ) as api:
        with pytest.raises(ArtifactTransportError, match="patched server"):
            await stream_action_artifact_zip(
                api, "gpakoh", "xloud", 17, file, redirect_origins=[],
            )
    assert requests == ["/api/v1/version"]
    assert file.getvalue() == b""


@pytest.mark.asyncio
async def test_large_gitea_version_evidence_is_bounded_before_archive_request():
    recorded = []

    def handler(request):
        recorded.append(request.url.path)
        if request.url.path.endswith("/version"):
            return httpx.Response(200, content=b"0" * 4096)
        return httpx.Response(200, content=make_zip())

    result = io.BytesIO()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=SOURCE,
        headers={"Authorization": f"token {PAT}"},
    ) as api:
        with pytest.raises(ArtifactTransportError, match="version response exceeds"):
            await stream_action_artifact_zip(
                api, "gpakoh", "xloud", 17, result, redirect_origins=[],
            )
    assert len(recorded) == 1
    assert recorded[0].endswith("/version")
    assert result.getvalue() == b""


@pytest.mark.asyncio
async def test_http_transport_failure_does_not_echo_signed_redirect_url_or_token():
    def api(req):
        return httpx.Response(302, headers={"Location": BLOB})

    def blob(req):
        raise httpx.ConnectError(f"connection failed for {BLOB} with {PAT}")

    with pytest.raises(ArtifactTransportError) as caught:
        await run(api, blob)
    assert "SENSITIVE-NEVER-LOG" not in str(caught.value)
    assert PAT not in str(caught.value)


def test_duplicate_zip_members_are_rejected_without_extraction():
    import warnings

    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("assets/config.txt", "first")
            archive.writestr("assets/config.txt", "second")
    with pytest.raises(ArtifactTransportError, match="unsafe member"):
        validate_zip_archive(output)


def test_case_insensitive_zip_collisions_are_rejected():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("Readme.txt", "first")
        archive.writestr("README.TXT", "second")
    with pytest.raises(ArtifactTransportError, match="unsafe member"):
        validate_zip_archive(output)

@pytest.mark.asyncio
async def test_native_gitea_client_exposes_only_a_private_destination_primitive():
    from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

    data = make_zip("preview/site/index.html")
    urls = []

    def handler(req):
        urls.append(req.url.path)
        assert req.headers["authorization"] == f"token {PAT}"
        if req.url.path.endswith("/version"):
            return httpx.Response(200, json={"version": "1.26.2"})
        return httpx.Response(200, content=data)

    async with GiteaClient(PAT) as gitea:
        await gitea._client.aclose()
        gitea._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=SOURCE,
            headers={"Authorization": f"token {PAT}"},
        )
        destination = io.BytesIO()
        metadata = await gitea.download_action_artifact_zip(
            "gpakoh", "xloud", 17, destination, redirect_origins=[],
        )
    assert urls == [
        "/api/v1/version",
        "/api/v1/repos/gpakoh/xloud/actions/artifacts/17/zip",
    ]
    assert metadata["sha256"] == hashlib.sha256(data).hexdigest()
    assert destination.getvalue() == data
    assert PAT not in str(metadata)
