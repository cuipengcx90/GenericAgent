#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绩效考评表格式闸门（performance_self_review_sop 配套，只读）

背景：2026-10-09 Q3 自评交付后用户手改了三处格式（加序号 / 换字体 / 精炼篇幅），
口径必须机械可查，否则下季必再犯。本闸门即那三处的判据单一源。

用法（**必须用 /opt/anaconda3/bin/python3**，系统 python3 无 openpyxl）：
  /opt/anaconda3/bin/python3 tools/perf_review_format_gate.py check <xlsx> [--json]
  /opt/anaconda3/bin/python3 tools/perf_review_format_gate.py rules
  /opt/anaconda3/bin/python3 tools/perf_review_format_gate.py selftest
  /opt/anaconda3/bin/python3 tools/perf_review_format_gate.py proof <xlsx>

退出码：0=全部通过 / 1=存在违规（逐条打印 rule id）/ 2=输入或环境错误（fail-closed，绝不静默放行）
阈值以 `rules` 子命令输出为准；SOP 里引用的数字须与之一致（RULES#21）。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

RULES = {
    "SHEET_NAME": "模板",
    "I_ROWS": [7, 8, 9, 10, 11],
    "I_FONT_FROM": "D7",          # 自评列字体 = 模板正文列字体（当季表格实测，勿写死字号）
    "C_FONT_FROM": "C7",          # C15/C16 字体 = C 列基准
    "I_ITEMS": [2, 6],            # 条数区间（Q3 定稿：I7 5 / I8 4 / I9 2）
    "I_ITEM_LEN": [10, 140],      # 单条字符数（Q3 定稿实测 30–126）
    "I_TOTAL_LEN": [150, 320],    # 整格字符数（Q3 定稿实测 194/220/258）
    "I_ITEM_RE": "^\\d+、.+[;\\uff1b\\u3002.]$",   # 条末标点:ASCII ; / 全角 ；(U+FF1B) / 。(U+3002)
    "C_CELLS": ["C15", "C16"],
    "C_HEAD": {
        "C15": "考核期间表现最好的三个方面：",
        "C16": "考核期间需改善或提升的三个方面：",
    },
    "C_ITEM_RE": r"^  \d+\. .+$",
    "C_ITEMS": 3,
    "C_TOTAL_LEN": [350, 620],    # Q3 定稿实测 486/380
    "H_RANGE": [110, 120],
    "H_AT_LEAST_ONE_TOP": True,
    "J_PLACEHOLDER": 100,
    "FORMULAS": ["H12", "J12", "H13"],
    "PARTS_MIN": 13,
    "PARTS_REQUIRED": ["xl/cellimages.xml", "xl/media/image1.png", "xl/sharedStrings.xml"],
    "PARTS_OPTIONAL": ["xl/_rels/cellimages.xml.rels"],
    "DISPIMG_RE": r"DISPIMG\(",
}


def font_of(cell):
    f = cell.font
    return {"name": f.name, "size": f.size, "bold": bool(f.bold)}


def same_font(a, b):
    return a["name"] == b["name"] and abs((a["size"] or 0) - (b["size"] or 0)) < 1e-6


def _txt(v):
    return "" if v is None else str(v)


def check_workbook(wb, path=None):
    """返回 findings 列表；每条 = {rule, cell, detail}"""
    out = []

    def add(rule, cell, detail):
        out.append({"rule": rule, "cell": cell, "detail": detail})

    ws = wb[wb.sheetnames[0]]
    if ws.title != RULES["SHEET_NAME"]:
        add("SHEET_NAME", ws.title, "sheet 名应为 %s" % RULES["SHEET_NAME"])

    # ---- 自评列（I7..I11）：字体随模板、分条编号、篇幅 ----
    i_font = font_of(ws[RULES["I_FONT_FROM"]])
    c_font = font_of(ws[RULES["C_FONT_FROM"]])
    i_lo, i_hi = RULES["I_ITEMS"]
    l_lo, l_hi = RULES["I_ITEM_LEN"]
    t_lo, t_hi = RULES["I_TOTAL_LEN"]
    item_re = re.compile(RULES["I_ITEM_RE"])
    filled = 0
    for r in RULES["I_ROWS"]:
        coord = "I%d" % r
        cell = ws[coord]
        text = _txt(cell.value).rstrip("\n")
        if not text.strip():
            continue
        filled += 1
        f = font_of(cell)
        if not same_font(f, i_font):
            add("I_FONT_MISMATCH", coord,
                "自评列字体 %s/%s 与模板列 %s 的 %s/%s 不一致" %
                (f["name"], f["size"], RULES["I_FONT_FROM"], i_font["name"], i_font["size"]))
        al = cell.alignment
        if not al.wrap_text or al.horizontal != "left" or al.vertical != "center":
            add("I_ALIGN", coord,
                "对齐应为 wrap=True/left/center，实为 wrap=%s/%s/%s" %
                (al.wrap_text, al.horizontal, al.vertical))
        lines = [l for l in text.split("\n") if l.strip() != ""]
        if not (i_lo <= len(lines) <= i_hi):
            add("I_ITEM_COUNT", coord, "条数 %d 不在 %d–%d（Q3 定稿 I7 5/I8 4/I9 2）"
                % (len(lines), i_lo, i_hi))
        for i, ln in enumerate(lines, 1):
            if not item_re.match(ln):
                add("I_ITEM_RE", coord, "第 %d 条不符 `序号、…；`：%s" % (i, ln[:40]))
            if not (l_lo <= len(ln) <= l_hi):
                add("I_ITEM_LEN", coord, "第 %d 条 %d 字不在 %d–%d" % (i, len(ln), l_lo, l_hi))
        if not (t_lo <= len(text) <= t_hi):
            add("I_TOTAL_LEN", coord, "整格 %d 字不在 %d–%d（领导嫌长即此列）"
                % (len(text), t_lo, t_hi))
    if filled == 0:
        add("I_EMPTY", "I7:I11", "自评列全空")

    # ---- C15/C16 ----
    c_lo, c_hi = RULES["C_TOTAL_LEN"]
    c_item_re = re.compile(RULES["C_ITEM_RE"])
    for coord in RULES["C_CELLS"]:
        cell = ws[coord]
        text = _txt(cell.value).rstrip("\n")
        if not text.strip():
            add("C_EMPTY", coord, "未填写")
            continue
        f = font_of(cell)
        if not same_font(f, c_font):
            add("C_FONT_MISMATCH", coord, "字体 %s/%s 与 C 列基准 %s/%s 不一致"
                % (f["name"], f["size"], c_font["name"], c_font["size"]))
        lines = text.split("\n")
        if lines[0] != RULES["C_HEAD"][coord]:
            add("C_HEAD", coord, "首行须为固定句「%s」，实为「%s」"
                % (RULES["C_HEAD"][coord], lines[0][:30]))
        heads = [l for l in lines if c_item_re.match(l)]
        if len(heads) != RULES["C_ITEMS"]:
            add("C_ITEMS", coord, "编号小标题 %d 条（应为 %d，形如「  1. 标题」）"
                % (len(heads), RULES["C_ITEMS"]))
        if not (c_lo <= len(text) <= c_hi):
            add("C_TOTAL_LEN", coord, "整格 %d 字不在 %d–%d" % (len(text), c_lo, c_hi))

    # ---- 评分 / 上级区 / 公式 ----
    h_lo, h_hi = RULES["H_RANGE"]
    hs = []
    for r in RULES["I_ROWS"]:
        v = ws["H%d" % r].value
        if v is None:
            continue
        if not isinstance(v, (int, float)) or not (h_lo <= v <= h_hi):
            add("H_RANGE", "H%d" % r, "自评分 %r 不在 %d–%d" % (v, h_lo, h_hi))
        else:
            hs.append(int(v))
    if RULES["H_AT_LEAST_ONE_TOP"] and hs and max(hs) < h_hi:
        add("H_NO_TOP", "H7:H11", "最高自评分 %d < %d（主战场域应给到顶档）" % (max(hs), h_hi))
    for r in RULES["I_ROWS"]:
        jv = ws["J%d" % r].value
        if jv is not None and jv != RULES["J_PLACEHOLDER"]:
            add("J_PLACEHOLDER", "J%d" % r, "上级评分占位应为 %s，实为 %r"
                % (RULES["J_PLACEHOLDER"], jv))
        kv = ws["K%d" % r].value
        if kv not in (None, ""):
            add("K_EMPTY", "K%d" % r, "上级区 K 列被填写：%r" % (kv,))
    for coord in RULES["FORMULAS"]:
        v = _txt(ws[coord].value)
        if not v.startswith("="):
            add("FORMULA_MISSING", coord, "公式丢失：%r" % (v[:40],))

    # ---- zip 部件（openpyxl 重写会静默丢内嵌签名图）----
    if path:
        try:
            names = zipfile.ZipFile(path).namelist()
        except Exception as e:
            add("PARTS_UNREADABLE", os.path.basename(path), repr(e))
            names = []
        if names:
            if len([n for n in names if not n.endswith("/")]) < RULES["PARTS_MIN"]:
                add("PARTS_MIN", os.path.basename(path),
                    "部件数 %d < %d（疑似丢图）" % (len(names), RULES["PARTS_MIN"]))
            for n in RULES["PARTS_REQUIRED"]:
                if n not in names:
                    add("PARTS_MISSING", os.path.basename(path), "缺部件 %s" % n)
            for n in RULES["PARTS_OPTIONAL"]:
                if n not in names:
                    add("PARTS_WARN", os.path.basename(path), "缺可选部件 %s" % n)
            try:
                sheet = [n for n in names if n.startswith("xl/worksheets/sheet")][0]
                body = zipfile.ZipFile(path).read(sheet).decode("utf-8", "ignore")
                if not re.search(RULES["DISPIMG_RE"], body):
                    add("DISPIMG_MISSING", os.path.basename(path),
                        "单元格内嵌签名图公式 DISPIMG(...) 缺失")
            except Exception:
                pass
    return out


def load_wb(path):
    try:
        import openpyxl  # noqa: F401
    except ImportError as e:
        raise SystemExit("rc=2 缺 openpyxl（用 /opt/anaconda3/bin/python3 跑）：%r" % (e,))
    import openpyxl
    if not os.path.isfile(path):
        raise SystemExit("rc=2 文件不存在：%s" % path)
    return openpyxl.load_workbook(path, data_only=False)


def cmd_check(args):
    wb = load_wb(args.xlsx)
    findings = check_workbook(wb, args.xlsx)
    hard = [f for f in findings if f["rule"] != "PARTS_WARN"]
    soft = [f for f in findings if f["rule"] == "PARTS_WARN"]
    if args.json:
        print(json.dumps({"file": args.xlsx, "findings": findings,
                          "hard": len(hard), "soft": len(soft)}, ensure_ascii=False, indent=1))
    else:
        print("== perf_review_format_gate: %s" % args.xlsx)
        ws = wb[wb.sheetnames[0]]
        print("   sheet=%s  自评列字体基准=%s  C列字体基准=%s"
              % (ws.title, font_of(ws[RULES["I_FONT_FROM"]]), font_of(ws[RULES["C_FONT_FROM"]])))
        for f in findings:
            print("   [%s] %s %s" % (f["rule"], f["cell"], f["detail"]))
        print("   HARD=%d SOFT=%d" % (len(hard), len(soft)))
        print("   VERDICT=%s" % ("PASS" if not hard else "FAIL"))
    return 1 if hard else 0


def cmd_rules(args):
    print(json.dumps(RULES, ensure_ascii=False, indent=1))
    return 0


def _fixture(path, i_text=None, c15=None, h7=120, j7=100, drop_formula=None, font_size=7.5):
    """按真实表格基准造夹具：D/B/G=等线 7.5（自评列锚此）、C=等线 9.0、C15/C16=等线 9.0 粗体"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = RULES["SHEET_NAME"]
    for coord in ("D7", "B7", "G7"):
        ws[coord] = "x"
        ws[coord].font = openpyxl.styles.Font(name="等线", size=7.5)
    ws["C7"] = 0.5
    ws["C7"].font = openpyxl.styles.Font(name="等线", size=9.0)
    ws["C15"].font = openpyxl.styles.Font(name="等线", size=9.0, bold=True)
    ws["C16"].font = openpyxl.styles.Font(name="等线", size=9.0, bold=True)
    body7 = i_text if i_text is not None else (
        "1、主导统一权限体系一、二期：以三层权限契约收敛各系统权限口径；\n"
        "2、主导供应链数据可信度治理闭环：完成复盘与修正，口径稳定可核；\n"
        "3、梳理模块级详细架构图及跨系统交互关系，覆盖四类文档；\n"
        "4、AI Coding 团队引领：把六个环节串成可复用的研发流水线；\n"
        "5、交付节奏稳定，任务交付准时率 100%，无重大线上事故；")
    for r in RULES["I_ROWS"]:
        c = ws["I%d" % r]
        c.value = body7
        c.font = openpyxl.styles.Font(name="等线", size=font_size)
        c.alignment = openpyxl.styles.Alignment(wrap_text=True, horizontal="left", vertical="center")
    c15_body = c15 if c15 is not None else (
        "考核期间表现最好的三个方面：\n"
        "  1. 平台架构设计的纵深推进\n"
        "  " + "甲" * 140 + "\n\n"
        "  2. AI 研发基建的体系化延伸\n"
        "  " + "乙" * 140 + "\n\n"
        "  3. 稳定性与数据可信度治理闭环\n"
        "  " + "丙" * 140)
    ws["C15"] = c15_body
    ws["C15"].alignment = openpyxl.styles.Alignment(wrap_text=True, horizontal="left", vertical="center")
    ws["C16"] = c15_body.replace("考核期间表现最好的三个方面：", RULES["C_HEAD"]["C16"])
    ws["C16"].alignment = openpyxl.styles.Alignment(wrap_text=True, horizontal="left", vertical="center")
    for r in RULES["I_ROWS"]:
        ws["H%d" % r] = h7 if r == 7 else 115
        ws["J%d" % r] = j7 if r == 7 else 100
    ws["H12"] = "=H7*C7+H8*C8"
    ws["J12"] = "=J7*C7+J8*C8"
    ws["H13"] = '=IF(J12<80,"C","S")'
    if drop_formula:
        ws[drop_formula] = None
    wb.save(path)
    return path


def _make_zip(path, parts):
    """伪造最小 xlsx 部件集；sheet1.xml 内嵌 DISPIMG 公式（真表特征）"""
    with zipfile.ZipFile(path, "w") as z:
        for n in parts:
            if n.startswith("xl/worksheets/sheet"):
                z.writestr(n, '<c><f>_xlfn.DISPIMG("ID_1",1)</f></c>'.encode("utf-8"))
            else:
                z.writestr(n, b"x")
    return path


def cmd_selftest(args):
    import openpyxl
    tmp = tempfile.mkdtemp(prefix="perf_gate_")
    checks = []

    def expect(name, wb_path, zip_path, want_rules, want_rc):
        wb = openpyxl.load_workbook(wb_path, data_only=False)
        fs = check_workbook(wb, zip_path)
        hard = [f["rule"] for f in fs if f["rule"] != "PARTS_WARN"]
        rc = 1 if hard else 0
        ok = (rc == want_rc) and all(w in hard for w in want_rules)
        checks.append((name, ok, "rc=%d rules=%s want=%s" % (rc, sorted(set(hard)), want_rules)))
        return fs

    full_parts = ["[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml",
                  "xl/cellimages.xml", "xl/media/image1.png", "xl/sharedStrings.xml",
                  "xl/_rels/cellimages.xml.rels"] + ["p%d" % i for i in range(7)]
    good_zip = _make_zip(os.path.join(tmp, "good.zip"), full_parts)

    # 正控：夹具应 PASS
    p = _fixture(os.path.join(tmp, "ok.xlsx"))
    expect("T1 正控 PASS", p, good_zip, [], 0)

    # NC1 字体漂到 9.0（本次真事故形态）
    p = _fixture(os.path.join(tmp, "nc1.xlsx"), font_size=9.0)
    expect("NC1 I 列字体不符", p, good_zip, ["I_FONT_MISMATCH"], 1)

    # NC2 段落式（无序号）——旧版写法
    old = ("  本季度点单平台的重心从「铺功能」转到「立规矩」：任务交付准时率 100%。\n\n"
           "  架构设计主导：牵头梳理模块级详细架构图与跨系统交互关系。" + "补" * 160)
    p = _fixture(os.path.join(tmp, "nc2.xlsx"), i_text=old)
    expect("NC2 无序号/段落式", p, good_zip, ["I_ITEM_RE"], 1)

    # NC3 条数越界（7 条）
    p = _fixture(os.path.join(tmp, "nc3.xlsx"),
                 i_text="\n".join(">%d、第 %d 条内容足够长以通过长度下限检查；" % (i, i) for i in range(1, 8)))
    expect("NC3 条数越界", p, good_zip, ["I_ITEM_COUNT", "I_ITEM_RE"], 1)

    # NC4 整格过短
    p = _fixture(os.path.join(tmp, "nc4.xlsx"), i_text="1、短；\n2、短；")
    expect("NC4 整格过短", p, good_zip, ["I_TOTAL_LEN"], 1)

    # NC5 单条超长
    p = _fixture(os.path.join(tmp, "nc5.xlsx"),
                 i_text="1、%s；\n2、交付节奏稳定，任务交付准时率 100%%；" % ("长" * 150))
    expect("NC5 单条超长", p, good_zip, ["I_ITEM_LEN"], 1)

    # NC6 C15 缺固定首行
    p = _fixture(os.path.join(tmp, "nc6.xlsx"), c15="表现最好的三个方面：\n  1. a\n" + "x" * 400)
    expect("NC6 C15 首行不符", p, good_zip, ["C_HEAD"], 1)

    # NC7 C15 只有两条
    p = _fixture(os.path.join(tmp, "nc7.xlsx"),
                 c15="考核期间表现最好的三个方面：\n  1. a\n" + "x" * 200 + "\n  2. b\n" + "y" * 200)
    expect("NC7 C15 条数不足", p, good_zip, ["C_ITEMS"], 1)

    # NC8 H 越界
    p = _fixture(os.path.join(tmp, "nc8.xlsx"), h7=125)
    expect("NC8 H 越界", p, good_zip, ["H_RANGE"], 1)

    # NC9 J 占位被覆盖
    p = _fixture(os.path.join(tmp, "nc9.xlsx"), j7=90)
    expect("NC9 J 占位被覆盖", p, good_zip, ["J_PLACEHOLDER"], 1)

    # NC10 公式丢失
    p = _fixture(os.path.join(tmp, "nc10.xlsx"), drop_formula="H12")
    expect("NC10 公式丢失", p, good_zip, ["FORMULA_MISSING"], 1)

    # NC11 丢内嵌签名图部件
    bad_zip = _make_zip(os.path.join(tmp, "bad.zip"),
                        [n for n in full_parts if n != "xl/media/image1.png"])
    p = _fixture(os.path.join(tmp, "nc11.xlsx"))
    expect("NC11 丢签名图部件", p, bad_zip, ["PARTS_MISSING"], 1)

    # NC12 条末无分号
    p = _fixture(os.path.join(tmp, "nc12.xlsx"),
                 i_text="1、第一条没有结尾分号\n2、交付节奏稳定，任务交付准时率 100%；")
    expect("NC12 条末标点", p, good_zip, ["I_ITEM_RE"], 1)

    # NC13 文件不存在 ⇒ rc=2 fail-closed
    try:
        load_wb(os.path.join(tmp, "nope.xlsx"))
        checks.append(("NC13 缺文件 fail-closed", False, "未抛错"))
    except SystemExit as e:
        checks.append(("NC13 缺文件 fail-closed", "rc=2" in str(e), str(e)[:60]))

    bad = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print("   [%s] %s | %s" % ("OK" if ok else "FAIL", name, detail))
    print("== selftest: %d/%d passed" % (len(checks) - len(bad), len(checks)))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not bad else 1


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cmd_proof(args):
    """只读自证：check 前后文件 sha256 不变"""
    before = _sha(args.xlsx)
    wb = load_wb(args.xlsx)
    findings = check_workbook(wb, args.xlsx)
    after = _sha(args.xlsx)
    hard = [f for f in findings if f["rule"] != "PARTS_WARN"]
    print("== proof: %s" % args.xlsx)
    print("   sha256_before=%s" % before)
    print("   sha256_after =%s" % after)
    print("   unchanged=%s  hard=%d" % (before == after, len(hard)))
    return 0 if before == after else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="绩效考评表格式闸门（只读）")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("xlsx")
    c.add_argument("--json", action="store_true")
    sub.add_parser("rules")
    sub.add_parser("selftest")
    pr = sub.add_parser("proof")
    pr.add_argument("xlsx")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "rules":
        return cmd_rules(args)
    if args.cmd == "selftest":
        return cmd_selftest(args)
    if args.cmd == "proof":
        return cmd_proof(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
