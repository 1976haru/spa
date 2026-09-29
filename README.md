# ShopSource Studio v0.1

100~200개 Shopify 자매몰 확장을 전제로 한 로컬 MASTER 소싱/상품관리 프로그램의 첫 버전입니다.

## v0.1에서 되는 것

- Spark `storage.zip` 또는 `storage/` 폴더 읽기
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

1. 이 폴더를 `D:\shop\shop-source-studio`에 둡니다.
2. `setup_windows.bat` 실행
3. 완료 후 `run_gui.bat` 실행

CLI 사용을 원하면 PowerShell에서:

```powershell
cd D:\shop\shop-source-studio
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

## 스토어별 가격 규칙 수정

`stores/001_cabin_tidy.json`의 `price_bands`를 수정한 뒤:

```powershell
shopsource add-store stores\001_cabin_tidy.json
shopsource classify --store 001
```

재수집하지 않고 MASTER DB를 다시 분류합니다.

## 데이터 파일

- DB: `data/shopsource.sqlite3`
- 내보내기: `exports/`
- 실제 DB와 export 파일은 Git에 올리지 않습니다.

## Spark Center

Spark Center에는 쓰기 작업을 하지 않습니다. 공식 API 또는 Import 계약이 확인되기 전까지
`connectors/spark_center`는 `CONTRACT_PENDING`을 유지합니다. 로컬 Spark dataset handoff의
capability는 `DATASET_LOAD_VERIFIED`이며 Shopify 업로드 지원을 의미하지 않습니다.

자세한 내용은 `docs/MASTER_SPEC.md`, `docs/SPARK_INTEGRATION.md`, `docs/ROADMAP.md`를 참고하세요.
