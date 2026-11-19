#!/usr/bin/env python3
"""Строгая локальная проверка CV: факты, требования, извлечение и актуальное ревью."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import subprocess
import tempfile
import zipfile
from xml.etree.ElementTree import ParseError
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
VERIFIED = {"user_confirmed", "code_checked", "primary_source_checked",
            "code_and_browser_checked", "user_confirmation_and_code_checked",
            "local_git_history_checked"}
REVIEW_CHECKS = {"claim_coverage", "chronology", "metric_boundaries",
                 "requirement_interpretation", "visual_readability", "recipient", "conditions"}
NOTICE = "PASS_LOCAL означает только прохождение локальных проверок. Настройки ATS работодателя неизвестны; результат не гарантирует просмотр, интервью или оффер и не разрешает отправку."


def norm(value):
    value = re.sub(r"(?<=\d)([–—-])\s*\n\s*(?=\d)", r"\1", value)
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def contains(text, value):
    return bool(re.search(r"(?<!\w)" + re.escape(norm(value)) + r"(?!\w)", norm(text)))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit(vacancy, text, profile, evidence, claim_map, review, fingerprints, aliases):
    if not isinstance(vacancy, dict) or not isinstance(profile, dict):
        raise ValueError("Вакансия и профиль должны быть JSON-объектами")
    if not isinstance(evidence, (dict, list)):
        raise ValueError("Evidence должен быть объектом или списком")
    if not isinstance(profile.get("skills", {}), dict) or not isinstance(profile.get("preferences", {}), dict):
        raise ValueError("Навыки и условия профиля должны быть объектами")
    if any(not isinstance(item, dict) for item in profile.get("skills", {}).values()):
        raise ValueError("Каждый навык профиля должен быть объектом")
    findings, checks = [], []

    def add(level, code, message, refs=None):
        findings.append({"level": level, "code": code, "message": message, "refs": refs or []})

    if not isinstance(text, str) or len(text.strip()) < 150:
        add("block", "unreadable_text", "Извлечено слишком мало текста; нужен читаемый документ, при необходимости OCR и ручная проверка.")
    if "\ufffd" in text:
        add("block", "replacement_characters", "В тексте есть символы замены; проверь шрифты и кодировку.")
    for field in ("name", "email"):
        expected = profile.get(field)
        if not isinstance(expected, str) or not expected.strip():
            add("block", "profile_identity_missing", "Не заполнено поле профиля: " + field)
        elif norm(expected) not in norm(text):
            add("review", "identity_not_found", "В тексте не найдено поле профиля: " + field + "; проверь формат или подтверждённую транслитерацию.")
    if re.search(r"\[(?:TODO|УТОЧНИТЬ|INSERT|ВСТАВИТЬ)[^\]]*\]|\{\{[^}]+\}\}", text, re.I):
        add("block", "draft_placeholders", "В CV остались маркеры незаполненного черновика.")
    if re.search(r"ignore (?:all |the )?(?:previous|prior) instructions|игнорируй (?:все )?предыдущие инструкции", text, re.I):
        add("block", "prompt_injection_text", "В CV обнаружена попытка инструктировать фильтр; удали её и проверь документ.")
    source = vacancy.get("text")
    if not isinstance(source, str) or not source.strip():
        add("block", "vacancy_source_missing", "Нужен исходный текст вакансии, а не только список ключей.")
        source = ""
    if not vacancy.get("company") or not vacancy.get("title") or not vacancy.get("captured_at"):
        add("review", "vacancy_metadata", "Нужны компания, роль и дата сохранения вакансии.")
    if urlsplit(str(vacancy.get("url", ""))).scheme != "https":
        add("review", "vacancy_url", "Не указана исходная HTTPS-ссылка на вакансию.")
    items = evidence.get("items", []) if isinstance(evidence, dict) else evidence
    valid_items = {e["id"]: e for e in items
                   if isinstance(e, dict) and e.get("id") and e.get("status") in VERIFIED and e.get("sources")}
    skills = {}
    for name, data in profile.get("skills", {}).items():
        ids = data.get("evidence", [])
        if ids and all(ident in valid_items for ident in ids):
            skills[name] = data
    requirements = vacancy.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        add("block", "requirements_missing", "Нужны явно разобранные требования с цитатами и обязательностью.")
        requirements = []
    used_ids = set()
    for req in requirements:
        if not isinstance(req, dict):
            add("block", "requirement_schema", "Требование должно быть объектом.")
            continue
        ident = req.get("id")
        if not isinstance(ident, str) or not ident or ident in used_ids:
            add("block", "requirement_id", "У требования нет уникального id.")
            continue
        used_ids.add(ident)
        quote = req.get("source_quote")
        if not isinstance(quote, str) or not quote.strip() or norm(quote) not in norm(source):
            add("block", "requirement_quote", "Цитата отсутствует в исходнике: " + ident)
            continue
        if type(req.get("required")) is not bool:
            add("block", "requirement_requiredness", "Обязательность должна быть явным bool: " + ident)
            continue
        kind = req.get("kind")
        status, refs, mentioned = "unknown", [], None
        if kind in {"skill", "experience"}:
            key, basis = req.get("skill"), req.get("basis", "practice")
            if not isinstance(key, str) or not key or basis not in {"practice", "commercial"}:
                add("block", "requirement_schema", "Неверный навык или basis: " + ident)
                continue
            if kind == "experience" and (type(req.get("min_months")) is not int or req["min_months"] < 0):
                add("block", "experience_schema", "Нужен неотрицательный целый min_months: " + ident)
                continue
            data = skills.get(key)
            if key == "frontend" and kind == "experience" and type(profile.get("commercial_frontend_months")) is int:
                data = {"commercial": True, "commercial_months": profile["commercial_frontend_months"],
                        "commercial_months_is_lower_bound": profile.get("commercial_frontend_months_is_lower_bound", False),
                        "evidence": [eid for eid, item in valid_items.items() if item.get("kind") == "commercial"]}
            if not data:
                status = "not_verified"
            else:
                refs = data["evidence"]
                commercial_refs = [eid for eid in refs if valid_items[eid].get("kind") == "commercial"
                                   and (key == "frontend" or key in valid_items[eid].get("skills", []))]
                if basis == "commercial" and (data.get("commercial") is not True or not commercial_refs):
                    status = "gap"
                elif kind == "experience":
                    minimum = req.get("min_months")
                    if type(minimum) is not int or minimum < 0:
                        add("block", "experience_schema", "Нужен неотрицательный целый min_months: " + ident)
                        continue
                    months = data.get("commercial_months") if basis == "commercial" else data.get("practice_months")
                    commercial = data.get("commercial_months")
                    if basis == "practice" and type(commercial) is int and commercial >= minimum:
                        status = "supported"
                    elif type(months) is int and months >= 0:
                        status = "supported" if months >= minimum else "unknown" if data.get("commercial_months_is_lower_bound") and basis == "commercial" else "gap"
                    else:
                        status = "unknown"
                else:
                    status = "supported"
            terms = aliases.get(key, [key])
            mentioned = any(contains(text, term) for term in terms)
            if mentioned and any(re.search(r"(?:не\s+(?:знаю|использовал|работал)|нет\s+опыта|no\s+experience)[^\n.!?]{0,45}" + re.escape(term), text, re.I) for term in terms):
                add("review", "negated_skill_mention", "Навык может упоминаться в отрицании: " + key)
            if status == "supported" and not mentioned:
                add("review", "supported_skill_missing", "Подтверждённый навык не найден в CV: " + key, refs)
        elif kind == "condition":
            field = req.get("field")
            if field not in {"remote_from_russia", "relocation", "mandatory_office_visits"} or type(req.get("value")) is not bool:
                add("block", "condition_schema", "Неизвестное условие или значение: " + ident)
                continue
            actual = profile.get("preferences", {}).get(field)
            status = "unknown" if actual is None else "supported" if actual == req["value"] else "gap"
        else:
            add("block", "requirement_kind", "Неизвестный вид требования: " + ident)
            continue
        checks.append({"id": ident, "status": status, "required": req["required"],
                       "source_quote": quote, "mentioned": mentioned, "evidence_ids": refs})
        if req["required"] and status != "supported":
            add("review", "required_gap", "Обязательное требование не подтверждено: " + ident + " (" + status + ")", refs)
    claims = claim_map.get("claims") if isinstance(claim_map, dict) else None
    if not isinstance(claims, list) or not claims:
        add("review", "claim_map_missing", "Нужна карта утверждений CV с evidence-id и границами.")
        claims = []
    mapped, claim_ids = [], set()
    for claim in claims:
        if not isinstance(claim, dict):
            add("block", "claim_schema", "Утверждение должно быть объектом.")
            continue
        ident, value, ids = claim.get("id"), claim.get("text"), claim.get("evidence_ids")
        if not isinstance(ident, str) or not ident or ident in claim_ids:
            add("block", "claim_id", "У утверждения нет уникального id.")
            continue
        claim_ids.add(ident)
        if not isinstance(value, str) or not value.strip() or norm(value) not in norm(text):
            add("block", "claim_not_in_resume", "Утверждение отсутствует в проверяемом CV: " + ident)
            continue
        scope = claim.get("scope")
        if scope == "profile_fact":
            pointers = claim.get("profile_refs")
            if not isinstance(pointers, list) or not pointers:
                add("block", "claim_profile_refs", "Нужны JSON Pointer на факты профиля: " + ident)
                continue
            try:
                for pointer in pointers:
                    if not isinstance(pointer, str) or not pointer.startswith("/"):
                        raise ValueError("JSON Pointer должен начинаться с /")
                    value_at_pointer = profile
                    for part in pointer[1:].split("/"):
                        part = part.replace("~1", "/").replace("~0", "~")
                        value_at_pointer = value_at_pointer[int(part)] if isinstance(value_at_pointer, list) else value_at_pointer[part]
                    if value_at_pointer is None:
                        raise ValueError("Неизвестный факт")
            except (ValueError, KeyError, TypeError, IndexError):
                add("block", "claim_profile_refs", "Не найден подтверждённый факт профиля: " + ident)
                continue
            ids = []
        elif not isinstance(ids, list) or not ids or any(not isinstance(eid, str) or eid not in valid_items for eid in ids):
            add("block", "claim_evidence", "Нет подтверждённого источника утверждения: " + ident)
            continue
        if scope not in {"commercial", "personal", "local_benchmark", "profile_fact"}:
            add("block", "claim_scope", "Нужна область commercial/personal/local_benchmark/profile_fact: " + ident)
        elif scope == "commercial" and not any(valid_items[eid].get("kind") == "commercial" for eid in ids):
            add("block", "personal_as_commercial", "Личный опыт представлен коммерческим: " + ident, ids)
        if scope == "local_benchmark":
            if not re.search(r"локальн|local", value, re.I) or not claim.get("measurement_limits"):
                add("block", "benchmark_limits", "Локальный benchmark требует явной области замера и measurement_limits: " + ident)
        mapped.append(value)
    if isinstance(claim_map, dict) and claim_map.get("unresolved"):
        add("review", "unresolved_claims", "В карте утверждений остаются неразрешённые вопросы фактов/хронологии.")
    metric_lines = [line.strip() for line in text.splitlines()
                    if re.search(r"\d\s*(?:%|→|мс\b|ms\b)|(?:\d[\d,.]*)\s*(?:млн|миллион|тысяч пользователей)", line, re.I)]
    for line in metric_lines:
        if not any(norm(line) in norm(value) or norm(value) in norm(line) for value in mapped):
            add("review", "metric_without_map", "Числовой результат не покрыт картой утверждений; проверь источник и контекст.")
    for key in aliases:
        if key not in skills and any(contains(text, term) for term in aliases[key]):
            add("review", "unverified_term", "Неподтверждённое упоминание " + key + "; оно может быть отрицанием, нужна смысловая проверка.")
    if vacancy.get("conditions_to_review"):
        add("review", "unresolved_conditions", "Остались условия вакансии для ручного разбора.")
    if not isinstance(review, dict):
        add("review", "review_missing", "Нужен независимый финальный review по текущим хэшам входов.")
    else:
        if review.get("hashes") != fingerprints:
            add("block", "stale_review", "Review относится к другим входам; повтори после изменений CV/вакансии/фактов/claim-map.")
        if review.get("verdict") != "approved" or review.get("independent") is not True or not review.get("reviewed_by"):
            add("review", "review_not_approved", "Не подтверждён независимый одобряющий review.")
        review_checks = review.get("checks")
        if not isinstance(review_checks, dict) or any(review_checks.get(key) is not True for key in REVIEW_CHECKS):
            add("review", "review_incomplete", "Не проверены все границы фактов, хронология, требования, вид документа и адресат.")
        if review.get("open_findings"):
            add("review", "review_findings", "В review остаются незакрытые замечания.")
    verdict = "BLOCKED" if any(f["level"] == "block" for f in findings) else "REVIEW" if any(f["level"] == "review" for f in findings) else "PASS_LOCAL"
    return {"schema_version": 1, "at": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict, "notice": NOTICE, "employer_pass_guaranteed": False,
            "submission_authorized": False, "hashes": fingerprints, "checks": checks,
            "findings": findings, "limitations": [
                "Цитата не доказывает правильность интерпретации must-have; это проверяет reviewer.",
                "Evidence-id и совпадение текста не доказывают семантическую полноту карты; нужен claim_coverage review.",
                "Парсер не проверяет надёжно скрытый текст, сложную вёрстку и все отрицания; нужен визуальный и смысловой review.",
                "Файл review — локальная аттестация, не криптографическое доказательство независимости.",
                "Проверка не знает скрытых вопросов анкеты, географических правил, зарплатного бюджета и внутренних настроек работодателя."]}


def load_foundation(config):
    path = Path(config["sources"]["ats_cli"]).expanduser()
    spec = importlib.util.spec_from_file_location("career_ats_foundation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_files(args, config, foundation=None):
    paths = {"vacancy": args.vacancy, "resume": args.resume,
             "profile": args.profile or Path(config["sources"]["profile"]).expanduser(),
             "evidence": args.evidence or Path(config["sources"]["evidence"]).expanduser()}
    if args.claims:
        paths["claims"] = args.claims
    snapshots = {name: Path(path).read_bytes() for name, path in paths.items()}
    fingerprints = {name: hashlib.sha256(value).hexdigest() for name, value in snapshots.items()}
    read = lambda name: json.loads(snapshots[name])
    review = json.loads(args.review.read_bytes()) if args.review else None
    foundation = foundation or load_foundation(config)
    with tempfile.TemporaryDirectory(prefix="career-preflight-") as temp:
        snapshot = Path(temp) / ("resume" + args.resume.suffix.lower())
        snapshot.write_bytes(snapshots["resume"])
        snapshot.chmod(0o600)
        text = foundation.extract(snapshot)
    report = audit(read("vacancy"), text, read("profile"), read("evidence"),
                   read("claims") if args.claims else None, review, fingerprints, foundation.ALIASES)
    changed = [name for name, path in paths.items() if sha(path) != fingerprints[name]]
    if changed:
        report["findings"].append({"level": "block", "code": "inputs_changed_during_run",
                                   "message": "Входы изменились во время проверки: " + ", ".join(changed), "refs": []})
        report["verdict"] = "BLOCKED"
    output = args.output.resolve()
    if output.is_relative_to(ROOT) and not output.is_relative_to(ROOT / "private"):
        raise ValueError("Личные результаты внутри проекта сохранять только в private/")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, value in (("report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
                        ("resume-extracted.txt", text)):
        target = output / name
        target.write_text(value)
        target.chmod(0o600)
    print(json.dumps({"verdict": report["verdict"], "findings": len(report["findings"]),
                      "report": str(output / "report.json"), "notice": NOTICE}, ensure_ascii=False))
    return {"PASS_LOCAL": 0, "REVIEW": 2, "BLOCKED": 3}[report["verdict"]]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("vacancy", "resume", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    for name in ("claims", "review", "profile", "evidence"):
        p.add_argument("--" + name, type=Path)
    args = p.parse_args()
    config = {"sources": {
        "ats_cli": ROOT / "career/scripts/career_ats.py",
        "profile": ROOT / "career/profile.json",
        "evidence": ROOT / "career/evidence.json",
    }}
    return run_files(args, config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile, ParseError) as exc:
        print("Ошибка входов/проверки: " + str(exc), file=sys.stderr)
        raise SystemExit(4)
