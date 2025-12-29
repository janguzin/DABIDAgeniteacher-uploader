# uploader.py — GENITEACHER 다중 세트 순차 업로드 + OCR 대기 + 자동 저장 + 다음세트 루프 (리뉴얼 대응)
from playwright.sync_api import sync_playwright
from pathlib import Path
from getpass import getpass
import os, re, sys, time

# ===================== 설정 =====================
UPLOAD_URL = "https://www.geniteacher.com/test-paper-upsert?id=0"  # 문제 생성 페이지
CATEGORIES = ["기출문제", "고3", "수학"]  # 클릭 순서 (기본값)
STORAGE_PATH = "geni_storage.json"  # 세션 파일
EDGE_CHANNEL = "msedge"  # Edge 실행
OCR_TIMEOUT_MS = 15 * 60 * 1000  # OCR 최대 대기(15분)
SAVE_DELAY_SEC = 5  # OCR 완료 후 저장까지 지연(초)

# ===================== 파일명 인식 =====================
ALLOWED_EXTS = {".pdf", ".doc", ".docx"}
PATTERN = re.compile(
    r"""
    ^
    (?P<base>\s*\d{4}_\d{1,2}_.+?)  # 2024_08_수학A
    _
    (?P<role>문제(?:지)?|해설(?:지)?|답(?:안|지)?)
    \s*(?:\(\d+\))?
    (?:\.[^.]+)+
    $
    """, re.IGNORECASE | re.VERBOSE
)

# ===================== 공용 유틸 =====================
def wait_until_enabled(locator, timeout_ms=60000):
    start = time.time()
    while time.time() - start < timeout_ms/1000:
        try:
            if locator.count() and locator.first.is_enabled() and locator.first.is_visible():
                return True
        except:
            pass
        time.sleep(0.2)
    return False

def js_force_click(page, locator):
    """DOM 겹침/레이어 이슈 대비: JS로 강제 클릭"""
    if not locator.count(): return False
    try:
        page.evaluate("(el)=>{el.scrollIntoView({block:'center'}); el.click();}", locator.first)
        return True
    except:
        try:
            locator.first.click(force=True)
            return True
        except:
            return False

def robust_click_by_text(page, texts, timeout_ms=120000):
    """여러 텍스트 후보를 순차 탐색해 강제 클릭"""
    end = time.time() + timeout_ms/1000
    # 1) CSS :has-text
    while time.time() < end:
        for t in texts:
            try:
                loc = page.locator(f"button:has-text('{t}')").first
                if loc.count():
                    wait_until_enabled(loc, 5000)
                    if js_force_click(page, loc): return True
            except:
                pass
        # 2) role 기반 (정규식)
        regex = re.compile("|".join([re.escape(t) for t in texts]))
        btn = page.get_by_role("button", name=regex)
        if btn.count():
            wait_until_enabled(btn, 5000)
            if js_force_click(page, btn): return True

        # 3) 링크 타입 백업
        link = page.get_by_role("link", name=regex)
        if link.count() and js_force_click(page, link): return True

        time.sleep(0.3)
    return False

# ===================== 스캔/정렬 =====================
def find_all_pairs_in_folder(folder: Path, debug=True):
    if not folder.exists(): raise FileNotFoundError(f"경로가 존재하지 않습니다: {folder}")
    if not folder.is_dir(): raise FileNotFoundError(f"폴더가 아니라 파일입니다: {folder}")

    by_base, skipped = {}, []
    for p in folder.iterdir():
        if not p.is_file(): continue
        if not any(sfx.lower() in ALLOWED_EXTS for sfx in p.suffixes):
            skipped.append((p.name, "확장자 제외")); continue
        m = PATTERN.match(p.name.strip())
        if not m:
            skipped.append((p.name, "이름 패턴 불일치")); continue
        base = m.group("base").strip()
        role = "문제" if "문제" in m.group("role") else "해설"
        d = by_base.setdefault(base, {})
        d.setdefault(role, p)

    pairs = []
    for base, d in by_base.items():
        if "문제" in d and "해설" in d:
            pairs.append((base, d["문제"], d["해설"]))
    pairs.sort(key=lambda x: x[0])

    if debug:
        print("▼ 스캔 결과 요약")
        for base, d in by_base.items():
            print(f"  - {base}: 문제={bool(d.get('문제'))}, 해설={bool(d.get('해설'))}")
        if skipped:
            print("▼ 스킵된 파일(이유):")
            for n, why in skipped:
                print(f"  * {n} -> {why}")
        print(f"▶ 업로드 대상 쌍: {len(pairs)}개")

    if not pairs:
        raise FileNotFoundError("업로드할 '(*)_문제' 와 '(*)_해설/답지' 쌍을 찾지 못했습니다.")
    return pairs

def infer_categories_from_folder(folder: Path):
    name = folder.name.strip()
    
    # 1. 폴더 이름에 '과학탐구'가 포함된 경우를 명시적으로 처리
    if "과학탐구" in name:
        return ["기출문제", "고3", "과학탐구"]
    
    # 2. 기존 로직: 구분자로 분할하여 카테고리 추론
    for sep in ["_", "-", " "]:
        if sep in name:
            parts = [p.strip().replace(" ", "") for p in name.split(sep) if p.strip()]
            if 1 < len(parts) <= 4:
                return parts
    
    # 3. 추론 실패 시 기본값 반환
    return CATEGORIES[:]

# ===================== 브라우저/네비 =====================
def get_browser_and_context(p):
    browser = p.chromium.launch(headless=False, channel=EDGE_CHANNEL)
    ctx = browser.new_context(storage_state=STORAGE_PATH) if os.path.exists(STORAGE_PATH) else browser.new_context()
    # 저장 대화창/알럿 자동 승인(간혹 confirm 뜨는 경우)
    ctx.on("dialog", lambda d: d.accept())
    return browser, ctx

def on_create_page(page) -> bool:
    field = page.locator("input[placeholder*='학습지명']")
    if field.count() == 0:
        field = page.locator("xpath=//label[contains(., '학습지명') or contains(., '문제지명')]/following::input[1]")
    return field.count() > 0

def try_login_if_needed(page, user, pw):
    if "login" not in page.url.lower(): return
    user = user or os.getenv("GENI_ID") or input("GENITEACHER 아이디: ").strip()
    pw   = pw   or os.getenv("GENI_PW") or getpass("GENITEACHER 비밀번호: ").strip()
    if not user or not pw: raise RuntimeError("아이디/비밀번호가 비었습니다.")
    print("[*] 로그인 페이지 감지 → 자동 로그인")
    page.fill("input[name*='email' i], input[name*='id' i], input[name*='user' i], input[type='text']", user)
    page.fill("input[type='password'], input[name*='pass' i]", pw)
    btn = page.get_by_role("button", name=re.compile("로그인|Login|Sign in", re.I))
    if btn.count() == 0: btn = page.locator("button[type='submit'], input[type='submit']").first
    btn.click()
    page.wait_for_load_state("networkidle")
    time.sleep(0.8)
    page.goto(UPLOAD_URL, wait_until="load"); page.wait_for_load_state("networkidle")

def reach_create_page(page, user, pw, max_steps=4):
    for _ in range(max_steps):
        if on_create_page(page): return
        page.goto(UPLOAD_URL, wait_until="load"); page.wait_for_load_state("networkidle")
        if on_create_page(page): return
        if "login" in page.url.lower():
            try_login_if_needed(page, user, pw)
            if on_create_page(page): return
        try:
            mgmt = (page.get_by_role("link", name=re.compile("^문제\s*관리$")) |
                    page.get_by_text("문제 관리", exact=True) |
                    page.locator("text=문제 관리").first)
            if mgmt.count(): mgmt.click(); page.wait_for_load_state("networkidle")
        except: pass
        try:
            create_btn = (page.get_by_role("link", name=re.compile("문제\s*(생성|등록|만들기)")) |
                          page.get_by_role("button", name=re.compile("문제\s*(생성|등록|만들기)")) |
                          page.locator("a[href*='test-paper-upsert']").first)
            if create_btn.count():
                create_btn.click(); page.wait_for_load_state("networkidle")
                if on_create_page(page): return
        except: pass
    raise RuntimeError("문제 생성 페이지 진입 실패(리뉴얼로 레이아웃 변경 가능).")

# ===================== OCR 대기 + 저장 =====================
BUSY_REGEX = re.compile(r"(OCR|변환|추출|처리 중|분석 중|업로드 중|페이지 인식)", re.I)

def _ocr_done_signal(page) -> bool:
    if page.get_by_role("button", name=re.compile("저장하기|저장|완료")).count() > 0: return True
    if page.locator("text=문항").count() > 0: return True
    if page.locator("[data-testid='question-list'], .question-list").count() > 0: return True
    return False

def wait_for_ocr_finish(page, timeout_ms=OCR_TIMEOUT_MS):
    start = time.time()
    try:
        page.get_by_text("문제 설정").first.wait_for(state="visible", timeout=120000)
    except: pass
    while True:
        try: page.wait_for_load_state("networkidle", timeout=8000)
        except: pass
        if _ocr_done_signal(page): return
        try: body = page.inner_text("body")[:200000]
        except: body = ""
        if body and not BUSY_REGEX.search(body): return
        if (time.time() - start) * 1000 > timeout_ms:
            raise TimeoutError("OCR 작업이 제한 시간 내에 끝나지 않았습니다.")
        time.sleep(1.0)

# ---- OCR 진입(문제등록) ----
def click_go_to_ocr(page, timeout_ms=120000):
    """
    리뉴얼 대응: 파일 업로드 완료 상태를 확인한 뒤
    '문제등록' 우선 클릭(불가 시 강제 클릭), 백업으로 '문항등록/업로드 시작/문제 등록'.
    """
    # 0) 업로드 값 채워졌는지 간단 확인(특정 사이트에서 버튼 활성화 트리거)
    try:
        file_inputs = page.locator("input[type='file']")
        for i in range(min(2, file_inputs.count())):
            page.wait_for_function("(el)=>!!el && el.files && el.files.length>0", arg=file_inputs.nth(i), timeout=30000)
    except: pass

    texts = ["문제등록", "문제 등록", "문항등록", "업로드 시작", "문제 등록"]
    ok = robust_click_by_text(page, texts, timeout_ms=timeout_ms)
    if not ok:
        raise RuntimeError("OCR로 이동할 '문제 등록' 버튼을 찾거나 클릭하지 못했습니다.")

    # 버튼 클릭 후 네트워크 안정화
    try: page.wait_for_load_state("networkidle", timeout=15000)
    except: pass

# ---- 저장 ----
def click_save(page, timeout_ms=120000):
    """
    상단 고정 '저장' / '저장하기' / '완료' 버튼을 강제 클릭.
    토스트/모달이 떠도 dialog auto-accept로 통과.
    """
    texts = ["저장하기", "저장", "완료"]
    ok = robust_click_by_text(page, texts, timeout_ms=timeout_ms)
    if not ok:
        raise RuntimeError("저장 버튼을 찾거나 클릭하지 못했습니다.")

    # 저장 처리 대기: 토스트/스피너가 사라지거나, 버튼이 다시 활성화되거나, URL/탭 상태가 안정화될 때까지
    t0 = time.time()
    while time.time() - t0 < 15:
        try: page.wait_for_load_state("networkidle", timeout=2000); break
        except: time.sleep(0.3)

# ===================== 메인 작업 =====================
def process_one_set(page, base, problem_file: Path, answer_file: Path, categories):
    """한 세트(문제/해설) 업로드 → OCR 진입 → OCR 대기 → (5초) → 저장."""
    reach_create_page(page, None, None)

    # 1) 문제지명 입력
    name_input = page.locator("input[placeholder*='학습지명']")
    if name_input.count() == 0:
        name_input = page.locator(
            "xpath=//label[contains(., '학습지명') or contains(., '문제지명')]/following::input[1]"
        )
    if name_input.count() == 0:
        raise RuntimeError("학습지명 입력 칸을 찾지 못했습니다.")
    name_input.first.click()
    name_input.first.fill(base)

    # 2) 카테고리 선택
    print(f"[*] 카테고리 선택: {' > '.join(categories)}")
    for cat in categories:
        loc = page.get_by_text(cat, exact=True).first
        loc.wait_for(state="visible", timeout=10000)
        js_force_click(page, loc)
        print(f"  - '{cat}' 클릭")
        time.sleep(0.2)

    # 3) 파일 업로드
    file_inputs = page.locator("input[type='file']")
    if file_inputs.count() < 2:
        raise RuntimeError("파일 업로드 인풋 2개(문제/해설)를 찾지 못했습니다.")
    file_inputs.nth(0).set_input_files(str(problem_file))
    file_inputs.nth(1).set_input_files(str(answer_file))

    # 4) [문제등록] (리뉴얼) 또는 [다음] (구버전) 클릭 → OCR 진입
    print("[*] OCR 변환 페이지 진입 시도(문제등록)...")
    click_go_to_ocr(page, timeout_ms=120000)

    # 5) OCR 완료 대기 → 5초 대기 → 저장
    print(f"[*] {base} : OCR 변환 대기 중...")
    wait_for_ocr_finish(page, timeout_ms=OCR_TIMEOUT_MS)
    print(f"[✓] {base} : OCR 완료 감지. {SAVE_DELAY_SEC}초 대기 후 저장...")
    time.sleep(SAVE_DELAY_SEC)

    # 6) 저장 클릭(상단 우측 버튼 등)
    click_save(page, timeout_ms=120000)
    print(f"[✓] {base} : 저장 완료.")

def run(folder: Path, ent_id=None, ent_pw=None, log_queue=None):
    pairs = find_all_pairs_in_folder(folder, debug=True)
    derived_categories = infer_categories_from_folder(folder)
    print(f"▶ 적용 카테고리: {' > '.join(derived_categories)}")

    with sync_playwright() as p:
        browser, context = get_browser_and_context(p)
        page = context.new_page()

        # 로그인/초기 진입
        page.goto(UPLOAD_URL, wait_until="load"); page.wait_for_load_state("networkidle")
        try_login_if_needed(page, ent_id, ent_pw)
        reach_create_page(page, ent_id, ent_pw)

        # 세트 루프
        for i, (base, prob, ans) in enumerate(pairs, 1):
            print(f"\n=== [{i}/{len(pairs)}] {base} 업로드 시작 ===")
            process_one_set(page, base, prob, ans, derived_categories)

            # 다음 세트 업로드를 위해 문제 생성 페이지로 복귀 (저장 후에도 안전하게)
            try:
                page.goto(UPLOAD_URL, wait_until="load")
                page.wait_for_load_state("networkidle")
            except:
                # 혹시 저장 후 다른 화면이라도 강제로 이동
                page.goto(UPLOAD_URL, wait_until="load")

        # 세션 저장
        context.storage_state(path=STORAGE_PATH)
        print("\n[✓] 모든 세트 업로드 및 저장 완료.")

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        arg = sys.argv[1].strip().strip('"').strip("'").rstrip("\\/")
        folder = Path(arg).expanduser().resolve()
    else:
        raw = input("업로드할 폴더 경로를 붙여넣고 엔터: ")
        folder = Path(raw.strip().strip('"').strip("'").rstrip("\\/")).expanduser().resolve()
    run(folder)