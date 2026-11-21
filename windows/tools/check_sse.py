#!/usr/bin/python3
"""Controlla che un eseguibile a 32 bit usi solo istruzioni disponibili su un Pentium III (MMX + SSE):
nessuna istruzione SSE2, SSE3, SSSE3, SSE4, AVX o successive.

Uso: check_sse.py FILE.exe   (codice di uscita 0 = ok, 1 = trovate istruzioni non ammesse)
"""
import collections
import re
import subprocess
import sys

# istruzioni SSE (1) che lavorano sui registri xmm: le uniche ammesse
SSE1_XMM = {
    "addps", "addss", "andnps", "andps", "cmpps", "cmpss", "comiss", "cvtpi2ps", "cvtps2pi", "cvtsi2ss",
    "cvtss2si", "cvttps2pi", "cvttss2si", "divps", "divss", "maxps", "maxss", "minps", "minss", "movaps",
    "movhlps", "movhps", "movlhps", "movlps", "movmskps", "movntps", "movss", "movups", "mulps", "mulss", "orps",
    "rcpps", "rcpss", "rsqrtps", "rsqrtss", "shufps", "sqrtps", "sqrtss", "subps", "subss", "ucomiss", "unpckhps",
    "unpcklps", "xorps", "cvtsi2ssl", "cvtsi2ssq",
}
CMP_PRED = r"(eq|lt|le|unord|neq|nlt|nle|ord)"
SSE1_CMP = re.compile(rf"^cmp{CMP_PRED}(ps|ss)$")
# istruzioni SSE2 (o successive) che non usano registri xmm
SSE2_OTHER = {"lfence", "mfence", "clflush", "movnti", "maskmovdqu", "pause_sse2_never"}


def main(path):
    out = subprocess.run(["objdump", "-d", "--no-show-raw-insn", path], capture_output=True, text=True,
                         check=True).stdout
    bad = collections.Counter()
    examples = {}
    func = "?"
    for line in out.splitlines():
        m = re.match(r"^[0-9a-f]+ <(.+)>:$", line)
        if m:
            func = m.group(1)
            continue
        m = re.match(r"^\s+[0-9a-f]+:\s+(\S+)\s*(.*)$", line)
        if not m:
            continue
        mnem, ops = m.group(1), m.group(2)
        if mnem in ("data16", "lock", "rep", "repz", "repnz", "notrack", "bnd") and ops:
            parts = ops.split(None, 1)
            mnem, ops = parts[0], parts[1] if len(parts) > 1 else ""
        problem = None
        if "%ymm" in ops or "%zmm" in ops or mnem.startswith("v") and "%xmm" in ops:
            problem = "AVX"
        elif "%xmm" in ops and mnem not in SSE1_XMM and not SSE1_CMP.match(mnem):
            problem = "SSE2+"
        elif mnem in SSE2_OTHER:
            problem = "SSE2"
        if problem:
            bad[(problem, mnem)] += 1
            examples.setdefault((problem, mnem), func)
    if bad:
        print(f"{path}: istruzioni non disponibili su Pentium III:")
        for (problem, mnem), n in bad.most_common():
            print(f"  {problem:6} {mnem:12} x{n}  (es. in {examples[(problem, mnem)]})")
        return 1
    print(f"{path}: OK, solo istruzioni x86 + MMX + SSE")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
