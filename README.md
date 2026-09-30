# ShopSource Studio v0.1

## Batch sourcing (Phase 2.6)

From UI V2 → Sourcing → API-free browser sourcing, choose a Store keyword and target count, then start a Batch. Review the Amazon search page and use the extension to capture its current DOM results. ShopSource queues unique candidates and opens detail pages sequentially (one tab at a time, with a minimum four-second interval); completed details can flow into MASTER and Store classification automatically. Search pagination remains manual. Resolve any Amazon confirmation screen yourself; the queue pauses without bypassing it. Start with five products and verify the Spark Center portal round-trip before scaling. See [docs/BATCH_SOURCING.md](docs/BATCH_SOURCING.md).

Phase 2.6.1 adds **미완료 상품 자동 처리**: completed captures are imported/classified first, remaining detail candidates are attached to the existing Store/keyword Batch, and the first detail page is opened. Phase 2.6.2 canonicalizes sponsored search URLs by ASIN and allows failed items to be retried. Phase 2.6.4 adds a background-driven detail-capture handshake and explicit recovery for a stuck opened item. After updating the project, open `chrome://extensions` (or `edge://extensions`) and press Reload for ShopSource Capture version 0.1.3, then refresh the open ShopSource UI tab once.

## API-free browser sourcing (Phase 2.5)

1. Start `run_ui_v2.bat`, choose a Store, and open Sourcing.
2. Create a Browser Capture pairing code in Settings and install the unpacked extension from `browser_extension/shopsource_capture` (`edge://extensions` or `chrome://extensions` → Developer mode → Load unpacked).
3. Configure extension URL `http://127.0.0.1:8081`, Store ID, and pairing code.
4. Open a recommended Amazon search, capture the visible page, then open up to five candidate detail pages and click the extension button on each.
5. Import detail-complete candidates to MASTER, review classifications, and create a Spark Center package. Upload only `ready/<package_id>`.

This browser workflow does not need a Keepa API key. Keepa remains optional for automated sourcing. CAPTCHA/robot checks stop capture; the extension never reads cookies, browser storage, login data, or credentials and does not paginate automatically. BROWSER_CAPTURE Spark mapping remains unverified until a manual portal round-trip.

100~200개 Shopify 자매몰 확장을 전제로 한 로컬 MASTER 소싱/상품관리 프로그램의 첫 버전입니다.

## v0.1에서 되는 것

- Spark `storage.zip` 또는 `storage/` 폴더 읽기
- 프로젝트 로컬 `source/amazon/inbox/`의 상품 JSON 가져오기
- Keepa-first 자동 후보 소싱(DRY RUN, token budget, checkpoint/resume)
- 여러 Spark 실행의 상품을 하나의 SQLite MASTER DB로 통합
- ASIN 기준 중복 제거, 출현 이력(job/file) 별도 보존
- 가격 때문에 상품을 삭제하지 않고 `PRIMARY / RESERVE_A/B/C / LOW_RESERVE / HIGH_RESERVE` 등으로 동적 분류
- 위험 키워드는 `REVIEW / RESTRICTED`로 보존
- Store Profile별 재분류: Cabin Tidy / Garage / Bathroom 샘플 포함
- 수동 override 상태를 재분류해도 유지할 수 있는 DB 구조
- CSV / JSON Export
- Windows 기본 Tkinter GUI
- Spark Center 연동용 Connector 인터페이스 자리 확보 (실제 연동 방식은 아직 미확정)
- 검증된 `datasets/<job_id>` 형식의 Spark handoff 폴더 자동 생성

## 가장 중요한 원칙

**수집 데이터는 MASTER DB에 최대한 보존합니다.**

예: $35 상품은 삭제하지 않습니다. Cabin Tidy의 현재 기준이 $40~100이면 `RESERVE_B`가 될 뿐입니다. 나중에 가격범위를 $30~120으로 바꾸고 다시 분류하면 기존 DB에서 자동으로 승격할 수 있습니다.

## Windows 설치

1. 저장소를 원하는 로컬 작업 폴더에 둡니다.
2. 저장소 루트에서 `setup_windows.bat`를 실행합니다.
3. 완료 후 `run_gui.bat`를 실행합니다.

기존 Tkinter GUI와 CLI는 계속 지원됩니다. 브라우저형 운영 화면은 별도 설치 후 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ui,similarity,credentials]"
shopsource-ui-v2
```

또는 저장소 루트에서 `run_ui_v2.bat`를 더블클릭합니다. UI V2는 로컬 `127.0.0.1`에서
실행되며 Dashboard, Store, Sourcing, Products, Packages, History, Settings의 7개 화면을
제공합니다. Tkinter 화면은 `run_gui.bat` / `shopsource-gui`로 언제든 사용할 수 있습니다.

CLI 사용을 원하면 PowerShell에서:

```powershell
cd <ShopSource Studio 저장소 경로>
.\.venv\Scripts\Activate.ps1
shopsource summary
```

## 첫 테스트

```powershell
shopsource import-spark "D:\경로\storage.zip"
shopsource classify --store 001
shopsource summary --store 001
shopsource export --store 001 --format csv
shopsource spark-handoff --store 001 --status PRIMARY --limit 5
```

Spark handoff 기본 출력은 `exports/spark_handoff/jobs/<job_id>/`이며 상품 JSON만 포함합니다.
Manifest와 validation report는 job 폴더 밖에 생성됩니다. 실제 사용자 테스트에서 5개 상품
dataset 폴더가 Spark UI에 정상 로드됐지만, Shopify 실제 업로드는 아직 검증되지 않았습니다.

## 초보자용 Spark Center 수동 업로드 순서

1. `run_gui.bat` 실행
2. `소싱 폴더 열기`를 눌러 `source/amazon/inbox/`에 상품 JSON을 넣기
3. `소싱 상품 가져오기`를 눌러 MASTER 저장 및 현재 Store 자동 분류
4. Status를 `PRIMARY`로 선택
5. 상품 수 입력(기본 50)
6. `Spark Center 업로드 폴더 만들기` 클릭
7. Validation PASS 확인 후 `폴더 열기`
8. 표시된 `ready/<package_id>` 폴더 하나만 Spark Center에 수동 업로드
9. 업로드를 수행했다면 `업로드 완료 표시` 클릭

기본 출력은 저장소 상대경로 `exports/spark_center/<store>/`입니다. `업로드 완료 표시`는
사용자 운영 기록일 뿐 Spark Center 포털이나 Shopify 성공을 자동 검증하지 않습니다.

## 자동 소싱 운영 순서

1. `run_gui.bat` 더블클릭 후 Store를 선택합니다.
2. `소싱 미리보기`로 keyword, 가격 범위, 예상 요청/token을 확인합니다. 미리보기는 network를 사용하지 않습니다.
3. `KEEPA_API_KEY` 환경변수를 설정하거나 `API 설정`에서 현재 GUI 세션 key를 입력합니다.
4. 첫 실제 실행은 Target `5`로 시작합니다. 확인 후 `50`, `500` 순서로 확대합니다.
5. 완료된 상품은 MASTER에 저장되고 현재 Store로 자동 분류됩니다.
6. PRIMARY를 사람이 검토한 뒤 Spark Center 업로드 폴더를 만듭니다.

CLI 미리보기:

```powershell
shopsource source-auto --store 001 --target 5 --dry-run
```

API key를 CLI 인자로 전달하는 옵션은 제공하지 않습니다. Keepa 원본은 Spark 원본과 구분되며,
Keepa→Spark payload mapping은 `KEEPA_TO_SPARK_MAPPING_UNVERIFIED` 상태입니다.
수동 `source/amazon/inbox/` 가져오기는 fallback으로 계속 사용할 수 있습니다.

## Keyword Studio / UI V2

Store 화면의 `추천 주제어 만들기`는 Store Profile seed와 MASTER/Keepa 제목의 n-gram에서
검색어 후보를 만듭니다. 점수와 추천 이유를 확인한 뒤 선택 항목을 recipe에 추가하거나 제외할
수 있습니다. Keepa 검증은 API token을 사용하므로 사용자가 10개 이하씩 확인 후 실행합니다.
추천/검증은 후보 탐색을 돕는 신호이며 판매 성과를 보장하지 않습니다.

NLP 기능은 선택 사항입니다. 설치하지 않으면 deterministic n-gram 및 lexical scoring으로
동작합니다. sentence-transformers는 모델이 이미 로컬에 있을 때만 사용하고 자동 다운로드하지
않습니다. 자세한 내용은 `docs/KEYWORD_INTELLIGENCE.md`, `docs/UI_V2.md`를 참고하세요.

## 스토어별 가격 규칙 수정

`stores/001_cabin_tidy.json`의 `price_bands`를 수정한 뒤:

```powershell
shopsource add-store stores\001_cabin_tidy.json
shopsource classify --store 001
```

재수집하지 않고 MASTER DB를 다시 분류합니다.

## 데이터 파일

- DB: `data/shopsource.sqlite3`
- 사용자 소싱 inbox: `source/amazon/inbox/`
- 내보내기: `exports/`
- 실제 DB, inbox 상품 JSON, export 파일은 Git에 올리지 않습니다.

## Spark Center

Spark Center에는 쓰기 작업을 하지 않습니다. 공식 API 또는 Import 계약이 확인되기 전까지
`connectors/spark_center`는 `CONTRACT_PENDING`을 유지합니다. 로컬 Spark dataset handoff의
capability는 `DATASET_LOAD_VERIFIED`이며 Shopify 업로드 지원을 의미하지 않습니다.
Spark Center 지원 측에서 외부 소싱 폴더의 수동 업로드 운영은 허용된다고 확인됐지만,
ShopSource JSON package의 실제 포털 round-trip은 아직 검증 전입니다.

자세한 내용은 `docs/MASTER_SPEC.md`, `docs/SPARK_INTEGRATION.md`, `docs/ROADMAP.md`를 참고하세요.
