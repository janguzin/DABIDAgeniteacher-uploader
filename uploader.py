# uploader.py — GENITEACHER 다중 세트(10월/11월 등) 순차 업로드 + OCR 대기 + 저장(5초 딜레이)
from playwright.sync_api import sync_playwright
from pathlib import Path
from getpass import getpass
import os, re, sys, time

# ===================== 설정 =====================
UPLOAD_URL = "https://www.geniteacher.com/test-paper-upsert?id=0"  # 문제 생성 페이지

STORAGE_PATH = "geni_storage.json"  # 세션 파일
EDGE_CHANNEL = "msedge"  # Edge 실행
OCR_TIMEOUT_MS = 15 * 60 * 1000  # OCR 최대 대기(15분)
SAVE_DELAY_SEC = 5  # OCR 완료 후 저장까지 지연(초)

# ===================== 파일명 인식 =====================
ALLOWED_EXTS = {".pdf", ".doc", ".docx"}
# 예: 2024_08_수학A_문제.pdf / 2024_08_수학A_해설.pdf
PATTERN = re.compile(
    r"""
    ^
    (?P<base>
        \s*\d{4}_\d{1,2}_.+?  # 년도_월_과목이름 (예: 2024_08_수학A)
    )
    _
    (?P<role>
        문제(?:지)?|해설(?:지)?|답(?:안|지)?
    )
    \s*(?:\(\d+\))?
    (?:\.[^.]+)+
    $
    """,
    re.IGNORECASE | re.VERBOSE
)

def find_all_pairs_in_folder(folder: Path, debug=True):
    """폴더 안의 모든 (base, 문제, 해설) 쌍을 반환. base 오름차순 정렬."""
    if not folder.exists(): raise FileNotFoundError(f"경로가 존재하지 않습니다: {folder}")
    if not folder.is_dir(): raise FileNotFoundError(f"폴더가 아니라 파일입니다: {folder}")

    by_base, skipped = {}, []
    for p in folder.iterdir():
        if not p.is_file():
            continue
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
        raise FileNotFoundError(
            "업로드할 '(*)_문제' 와 '(*)_해설(=해설지/답지/답안)' 쌍을 찾지 못했습니다."
        )
    return pairs



def get_browser_and_context(p):
    """세션 파일(STORAGE_PATH) 있으면 재사용, 없으면 새 컨텍스트."""
    browser = p.chromium.launch(headless=False, channel=EDGE_CHANNEL)
    if os.path.exists(STORAGE_PATH):
        ctx = browser.new_context(storage_state=STORAGE_PATH)
    else:
        ctx = browser.new_context()
    return browser, ctx

def on_create_page(page) -> bool:
    """문제 생성 페이지인지 판별: '학습지명/문제지명' 인풋 존재 확인"""
    field = page.locator("input[placeholder*='학습지명']")
    if field.count() == 0:
        field = page.locator(
            "xpath=//label[contains(., '학습지명') or contains(., '문제지명')]/following::input[1]"
        )
    return field.count() > 0

def try_login_if_needed(page, user, pw):
    """
    로그인 페이지면 자동 로그인 후 문제 생성 페이지로 이동.
    GUI에서 전달받은 아이디/비밀번호를 사용.
    """
    if "login" not in page.url.lower():
        return

    if not user:
        user = os.getenv("GENI_ID")
    if not pw:
        pw = os.getenv("GENI_PW")
    
    if not user:
        user = input("GENITEACHER 아이디: ").strip()
    if not pw:
        pw = getpass("GENITEACHER 비밀번호: ").strip()
        
    if not user or not pw:
        raise RuntimeError("아이디/비밀번호가 비었습니다.")

    print("[*] 로그인 페이지 감지 → 자동 로그인")
    page.fill("input[name*='email' i], input[name*='id' i], input[name*='user' i], input[type='text']", user)
    page.fill("input[type='password'], input[name*='pass' i]", pw)
    btn = page.get_by_role("button", name=re.compile("로그인|Login|Sign in", re.I))
    if btn.count() == 0:
        btn = page.locator("button[type='submit'], input[type='submit']").first
    btn.click()

    page.wait_for_load_state("networkidle")
    time.sleep(0.8)
    page.goto(UPLOAD_URL, wait_until="load")
    page.wait_for_load_state("networkidle")

def reach_create_page(page, user, pw, max_steps=4):
    """
    어디로 리다이렉트되든 최종적으로 '문제 생성' 페이지로 진입.
    """
    for _ in range(max_steps):
        if on_create_page(page):
            return
        if on_create_page(page):
            return
        if "login" in page.url.lower():
            try_login_if_needed(page, user, pw)
            if on_create_page(page):
                return
        try:
            mgmt = (page.get_by_role("link", name=re.compile("^문제\s*관리$")) |
                    page.get_by_text("문제 관리", exact=True) |
                    page.locator("text=문제 관리").first)
            if mgmt.count():
                mgmt.click(); page.wait_for_load_state("networkidle")
        except Exception:
            pass
        try:
            create_btn = (page.get_by_role("link", name=re.compile("문제\s*(생성|등록|만들기)")) |
                          page.get_by_role("button", name=re.compile("문제\s*(생성|등록|만들기)")) |
                          page.locator("a[href*='test-paper-upsert']").first)
            if create_btn.count():
                create_btn.click(); page.wait_for_load_state("networkidle")
                if on_create_page(page): return
        except Exception:
            pass
    raise RuntimeError("문제 생성 페이지로 이동하지 못했습니다. 사이트 메뉴/레이아웃이 변경된 듯합니다.")

# ===================== OCR 대기 + 저장 =====================
BUSY_REGEX = re.compile(r"(OCR|변환|추출|처리 중|분석 중)", re.I)

def _ocr_done_signal(page) -> bool:
    if page.get_by_role("button", name=re.compile("저장하기|저장|완료")).count() > 0:
        return True
    if page.locator("text=문항").count() > 0:
        return True
    if page.locator("[data-testid='question-list'], .question-list").count() > 0:
        return True
    return False

def wait_for_ocr_finish(page, timeout_ms=OCR_TIMEOUT_MS):
    start = time.time()
    try:
        page.get_by_text("문제 설정").first.wait_for(state="visible", timeout=120000)
    except:
        pass
    while True:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except:
            pass
        if _ocr_done_signal(page):
            return
        body = ""
        try:
            body = page.inner_text("body")[:200000]
        except:
            pass
        if body and not BUSY_REGEX.search(body):
            return
        if (time.time() - start) * 1000 > timeout_ms:
            raise TimeoutError("OCR 작업이 제한 시간 내에 끝나지 않았습니다.")
        time.sleep(1.2)

def wait_until_enabled(locator, timeout_ms=60000):
    start = time.time()
    while time.time() - start < timeout_ms/1000:
        try:
            if locator.is_enabled(): return True
        except:
            pass
        time.sleep(0.3)
    return False

def click_save(page):
    candidates = [
        "button:has-text('저장하기')",
        "button:has-text('저장')",
        "button:has-text('완료')",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel)
            if btn.count():
                if not wait_until_enabled(btn.first, 120000):
                    pass
                btn.first.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass
                return
        except:
            continue
    btn = page.get_by_role("button", name=re.compile("저장하기|저장|완료"))
    if btn.count():
        btn.first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except:
            pass
        return
    raise RuntimeError("저장 버튼을 찾지 못했습니다.")

def process_one_set(page, base, problem_file: Path, answer_file: Path):
    """한 세트(문제/해설) 업로드 → 다음 → OCR 대기 → (5초) → 저장."""
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
    


    # 3) 파일 업로드
    file_inputs = page.locator("input[type='file']")
    file_inputs.nth(0).set_input_files(str(problem_file))
    file_inputs.nth(1).set_input_files(str(answer_file))

    # 4) [다음] 클릭
    next_btn = page.get_by_role("button", name=re.compile("^다음$"))
    if not wait_until_enabled(next_btn, timeout_ms=120000):
        print("경고: [다음] 버튼이 아직 비활성입니다. 그래도 클릭 시도합니다.")
    next_btn.click()

    # 5) OCR 완료 대기 → 5초 대기 → 저장
    print(f"[*] {base} : OCR 변환 대기 중...")
    wait_for_ocr_finish(page, timeout_ms=OCR_TIMEOUT_MS)
    print(f"[✓] {base} : OCR 완료 감지. {SAVE_DELAY_SEC}초 대기 후 저장합니다...")
    time.sleep(SAVE_DELAY_SEC)
    click_save(page)
    print(f"[✓] {base} : 저장 완료.")

def run(folder: Path, ent_id=None, ent_pw=None, log_queue=None):
    pairs = find_problem_answer_pairs(folder)

    print(f"▶ 적용 카테고리: {' > '.join(derived_categories)}")

    with sync_playwright() as p:
        browser, context = get_browser_and_context(p)
        page = context.new_page()

        page.goto(UPLOAD_URL, wait_until="load")
        page.wait_for_load_state("networkidle")
        try_login_if_needed(page, ent_id, ent_pw)
        reach_create_page(page, ent_id, ent_pw)

        for i, (base, prob, ans) in enumerate(pairs, 1):
            print(f"\n=== [{i}/{len(pairs)}] {base} 업로드 시작 ===")
            process_one_set(page, base, prob, ans, derived_categories)
            
            page.goto(UPLOAD_URL, wait_until="load")
            page.wait_for_load_state("networkidle")

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

# 카테고리 선택 완료 감지 함수 
def wait_until_category_selection_done(page, timeout_ms=10 * 60 * 1000):
    """
    사진 기준:
    - 선택 안 된 카테고리: 회색 + 아이콘
    - 선택 된 카테고리: 파란색 − 아이콘

    모든 카테고리에서 (+) 버튼이 사라질 때까지 대기
    """
    start = time.time()

    print("[*] 사용자가 카테고리 선택을 완료할 때까지 대기 중...")

    while True:
        unselected = page.locator("button:has-text('+')")
        if unselected.count() == 0:
            print("[✓] 모든 카테고리 선택 완료 감지")
            return

        if (time.time() - start) * 1000 > timeout_ms:
            raise TimeoutError("카테고리 선택이 완료되지 않았습니다.")

        time.sleep(0.5)

# 문제/해설 자동 매칭 함수
def find_problem_answer_pairs(folder: Path):
    """
    *_문제.xxx / *_해설.xxx 자동 매칭
    """
    problems = {}
    answers = {}

    for p in folder.iterdir():
        if not p.is_file():
            continue

        name = p.stem
        if name.endswith("_문제"):
            base = name[:-3]
            problems[base] = p
        elif name.endswith("_해설"):
            base = name[:-3]
            answers[base] = p

    pairs = []
    for base in problems:
        if base in answers:
            pairs.append((base, problems[base], answers[base]))

    if not pairs:
        raise FileNotFoundError("문제/해설 파일 쌍을 찾지 못했습니다.")

    return sorted(pairs, key=lambda x: x[0])

def run(folder: Path, ent_id=None, ent_pw=None, log_queue=None):
    pairs = find_problem_answer_pairs(folder)

    with sync_playwright() as p:
        browser, context = get_browser_and_context(p)
        page = context.new_page()

        page.goto(UPLOAD_URL, wait_until="load")
        try_login_if_needed(page, ent_id, ent_pw)
        reach_create_page(page, ent_id, ent_pw)

        # 🔥 여기 핵심
        wait_until_category_selection_done(page)

        for i, (base, prob, ans) in enumerate(pairs, 1):
            print(f"\n=== [{i}/{len(pairs)}] {base} 업로드 시작 ===")
            process_one_set(page, base, prob, ans)

            page.goto(UPLOAD_URL, wait_until="load")
            page.wait_for_load_state("networkidle")

        context.storage_state(path=STORAGE_PATH)
        print("\n[✓] 모든 세트 업로드 완료")





