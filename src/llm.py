"""LLM çağrısı tek bir yerde: backend değiştirmek tek satırlık iştir.

Backend'ler:
  claude_cli    Claude Code CLI, abonelik token'ı (CLAUDE_CODE_OAUTH_TOKEN) ile
  anthropic_api Anthropic API, ANTHROPIC_API_KEY ile
  mock          Ağ ve ücret yok; hattı sınamak için deterministik sahte yanıt
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from .util import extract_json


class LLM:
    def __init__(self, backend: str, select_model: str, summarize_model: str):
        self.backend = backend
        self.models = {"select": select_model, "summarize": summarize_model}

    # -- genel arayüz --------------------------------------------------
    def json(self, stage: str, system: str, prompt: str):
        """Yanıtı JSON olarak döndürür; bozuksa bir kez yeniden dener."""
        last_err: Exception | None = None
        for attempt in range(2):
            text = self._complete(stage, system, prompt)
            try:
                return extract_json(text)
            except ValueError as exc:
                last_err = exc
                prompt += "\n\nÖnceki yanıtın geçerli JSON değildi. Yalnızca JSON döndür."
        raise RuntimeError(f"{stage}: geçerli JSON alınamadı ({last_err})")

    # -- backend'ler ---------------------------------------------------
    def _complete(self, stage: str, system: str, prompt: str) -> str:
        if self.backend == "mock":
            return _mock(stage, prompt)
        if self.backend == "anthropic_api":
            return self._api(stage, system, prompt)
        if self.backend == "claude_cli":
            return self._cli(stage, system, prompt)
        raise ValueError(f"Bilinmeyen LLM backend: {self.backend}")

    def _api(self, stage: str, system: str, prompt: str) -> str:
        import anthropic  # yalnızca bu backend'de gerekir

        client = anthropic.Anthropic()  # ANTHROPIC_API_KEY ortamdan okunur
        msg = client.messages.create(
            model=self.models[stage], max_tokens=8000, system=system,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def _cli(self, stage: str, system: str, prompt: str) -> str:
        # Not: CLI bayrakları bu depoda henüz canlı doğrulanmadı; ilk Actions çalışmasında sınayın.
        full = f"{system}\n\n{prompt}"
        proc = subprocess.run(
            ["claude", "-p", "--model", self.models[stage], "--output-format", "text"],
            input=full, text=True, capture_output=True, timeout=900,
            env={**os.environ})
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI hata verdi: {proc.stderr.strip()[:500]}")
        return proc.stdout


def _mock(stage: str, prompt: str) -> str:
    """Hattı uçtan uca sınamak için sahte yanıt."""
    if stage == "select":
        ids = re.findall(r'"id":\s*"([0-9a-f]{8})"', prompt)
        n = int(re.search(r"EN FAZLA (\d+)", prompt).group(1))
        picks = [{"id": i, "score": max(1, 5 - k // 2), "neden": "sahte eleme"}
                 for k, i in enumerate(ids[:n])]
        return json.dumps(picks, ensure_ascii=False)
    # özet aşaması
    items = []
    for m in re.finditer(r'"id":\s*"([0-9a-f]{8})".*?"title":\s*"((?:[^"\\]|\\.)*)"', prompt, re.S):
        title = json.loads(f'"{m.group(2)}"')
        items.append({"id": m.group(1), "baslik_tr": f"[TR] {title}",
                      "ozet_tr": "Bu bir sahte özettir; gerçek çalıştırmada kaynak metne dayanır.",
                      "neden_onemli": "Sahte gerekçe."})
    return json.dumps(items, ensure_ascii=False)
