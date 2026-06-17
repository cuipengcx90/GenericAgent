"""
http_utils.py — HTTP请求工具（基于 httpx + asyncio）
能力节点：同步/异步GET/POST + 超时重试 + 代理 + cookie/header注入 + 流式下载 + 批量并发
"""
from __future__ import annotations
import asyncio, pathlib, time
from typing import Any, Dict, List, Optional, Union
import httpx

_DEFAULT_TIMEOUT = 15.0
_DEFAULT_RETRIES = 3
_DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def _ckw(timeout, proxy, headers, cookies, verify=True):
    kw = {"timeout": timeout, "verify": verify,
          "headers": {"User-Agent": _DEFAULT_UA, **(headers or {})}}
    if cookies: kw["cookies"] = cookies
    if proxy:   kw["proxies"] = {"http://": proxy, "https://": proxy}
    return kw

def get(url, params=None, headers=None, cookies=None, proxy=None,
        timeout=_DEFAULT_TIMEOUT, retries=_DEFAULT_RETRIES, verify=True):
    """同步GET，指数退避重试。"""
    kw = _ckw(timeout, proxy, headers, cookies, verify); exc = None
    for i in range(1, retries+1):
        try:
            with httpx.Client(**kw) as c: return c.get(url, params=params)
        except Exception as e:
            exc = e
            if i < retries: time.sleep(0.4 * i)
    raise exc

def post(url, data=None, json=None, headers=None, cookies=None, proxy=None,
         timeout=_DEFAULT_TIMEOUT, retries=_DEFAULT_RETRIES, verify=True):
    """同步POST，支持form-data或JSON body，含重试。"""
    kw = _ckw(timeout, proxy, headers, cookies, verify); exc = None
    for i in range(1, retries+1):
        try:
            with httpx.Client(**kw) as c: return c.post(url, data=data, json=json)
        except Exception as e:
            exc = e
            if i < retries: time.sleep(0.4 * i)
    raise exc

def get_json(url, params=None, headers=None, timeout=_DEFAULT_TIMEOUT):
    """快捷JSON GET，直接返回解析后Python对象。"""
    r = get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status(); return r.json()

def download_file(url, output_path, headers=None, cookies=None, proxy=None,
                  timeout=60.0, chunk_size=8192, verify=True):
    """流式下载文件到本地，返回Path对象。"""
    out = pathlib.Path(output_path); out.parent.mkdir(parents=True, exist_ok=True)
    kw = _ckw(timeout, proxy, headers, cookies, verify)
    with httpx.Client(**kw) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=chunk_size): f.write(chunk)
    return out

async def async_get(url, params=None, headers=None, cookies=None, proxy=None,
                    timeout=_DEFAULT_TIMEOUT, retries=_DEFAULT_RETRIES, verify=True):
    """异步GET，含指数退避重试。"""
    kw = _ckw(timeout, proxy, headers, cookies, verify); exc = None
    for i in range(1, retries+1):
        try:
            async with httpx.AsyncClient(**kw) as c: return await c.get(url, params=params)
        except Exception as e:
            exc = e
            if i < retries: await asyncio.sleep(0.4 * i)
    raise exc

async def async_post(url, data=None, json=None, headers=None, cookies=None,
                     proxy=None, timeout=_DEFAULT_TIMEOUT, retries=_DEFAULT_RETRIES, verify=True):
    """异步POST，支持form-data或JSON body，含重试。"""
    kw = _ckw(timeout, proxy, headers, cookies, verify); exc = None
    for i in range(1, retries+1):
        try:
            async with httpx.AsyncClient(**kw) as c: return await c.post(url, data=data, json=json)
        except Exception as e:
            exc = e
            if i < retries: await asyncio.sleep(0.4 * i)
    raise exc

async def batch_async_get(urls, params=None, headers=None,
                          timeout=_DEFAULT_TIMEOUT, concurrency=5):
    """批量并发GET，返回[{url,status,ok,elapsed_ms}]。"""
    sem = asyncio.Semaphore(concurrency)
    kw = _ckw(timeout, None, headers, None)
    async def _fetch(client, url):
        async with sem:
            t0 = time.time()
            try:
                r = await client.get(url, params=params)
                return {"url":url,"status":r.status_code,"ok":r.is_success,
                        "elapsed_ms":round((time.time()-t0)*1000)}
            except Exception as e:
                return {"url":url,"status":None,"ok":False,"error":str(e),
                        "elapsed_ms":round((time.time()-t0)*1000)}
    async with httpx.AsyncClient(**kw) as client:
        return await asyncio.gather(*[_fetch(client,u) for u in urls])
