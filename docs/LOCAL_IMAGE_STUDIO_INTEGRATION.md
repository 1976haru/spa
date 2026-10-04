# 로컬 이미지 스튜디오와 프롬프트 사용법

ShopSource는 먼저 스토어·브랜드·컬렉션 계획을 바탕으로 배너, 컬렉션, 카테고리 카드, 헤더/소개 섹션 프롬프트를 만듭니다. 프롬프트 생성과 파일 내보내기는 로컬 동작이며 유료 이미지 API를 부르지 않습니다.

## 초보자 사용 순서

1. Cabin Tidy를 선택하고 홈페이지에서 `홈페이지 자동 설계`를 눌러 로컬 계획을 준비합니다.
2. `프롬프트 자동 생성`을 누르고 원하는 자산 카드를 펼칩니다.
3. `복사`를 눌러 ChatGPT 등 이미지 도구에 붙여넣습니다. 기본 Hero는 글자·로고가 없는 그림이며, 제목과 버튼은 Shopify 테마에서 별도로 표시합니다.
4. 생성된 PNG, JPEG 또는 WebP 파일을 Hero 업로드나 컬렉션 이미지 업로드에서 선택합니다.
5. 파일 검사에서 크기, 비율, 투명 배경 경고를 확인합니다. 경고가 있어도 파일을 임의로 고치지 않으며, 실제 화면 crop을 검토하세요.
6. 자산을 직접 승인한 뒤 homepage/theme 미리보기를 확인합니다. 실제 Shopify/Theme 변경은 별도 사람 확인 후에만 가능합니다.

`전체 프롬프트 파일 내보내기`는 `exports/asset_prompts/<store>/<run_id>/` 아래에 Hero, collection, category, header Markdown 파일, `asset_prompts.json`, `copy_paste_bundle.txt`를 저장합니다. 복붙 묶음은 항목별 plain text입니다.

## 로컬 YouTubeSum 연결

ShopSource는 Python module을 직접 import하지 않습니다. 설정한 Python으로 `D:\03_youtubesum\shopsource_bridge.py`를 subprocess 실행하고 JSON job/result 계약으로 통신합니다. 기본 제안 경로는 `D:\03_youtubesum`입니다. 앱 설정 환경변수는 `SHOP_SOURCE_YOUTUBESUM_PATH`, `SHOP_SOURCE_YOUTUBESUM_PYTHON`, `SHOP_SOURCE_YOUTUBESUM_BRIDGE`, `SHOP_SOURCE_IMAGE_OUTPUT`, `SHOP_SOURCE_IMAGE_TIMEOUT`, `SHOP_SOURCE_IMAGE_CANDIDATES`입니다.

현재 YouTubeSum 저장소에는 썸네일 편집, crop/resize와 기존 Image Bridge가 있습니다. 이를 새 이미지 생성 모델로 오인하지 않습니다. 실제 모델이 설정되지 않은 상태는 `WAITING_FOR_CONFIGURATION`이며 ShopSource는 prompt-only와 직접 업로드로 계속 진행합니다. 브리지가 연결된 후에도 capabilities에서 `generator.ready=true`를 반환해야 생성 버튼이 활성화됩니다. 승인된 참고 이미지의 비생성 crop은 가능한 경우에도 `NON_GENERATIVE_DERIVATIVE`로 표시합니다.

브리지 명령은 `--health`, `--capabilities`, `--doctor`, `--job <job.json> --result <result.json>`입니다. 결과 후보는 job의 출력 폴더 밖에 있을 수 없고 SHA-256과 실제 이미지 크기를 검사합니다. UI 자동화나 Tkinter 클릭은 사용하지 않습니다.

## Pillow 오류 복구

`pyproject.toml`에는 `Pillow>=10`이 필요 패키지로 선언돼 있습니다. `run_ui_v2.bat`은 `.venv\Scripts\python.exe`를 사용합니다. 현재 로컬 검사에서는 이 interpreter에 Pillow가 없고 NiceGUI는 설치돼 있었습니다. 이미지 화면의 `이미지 엔진 연결 확인`에서 `환경 자동 복구`를 누르면 같은 interpreter로 `python -m pip install -e .[ui]`를 실행하고, 완료 뒤 앱 재시작을 안내합니다. 이 작업은 사용자가 버튼을 눌렀을 때만 실행됩니다.

## 제한사항

- 생성 모델 미설정 시 실제 생성은 제공하지 않습니다. prompt-only fallback이 기본입니다.
- 외부 ChatGPT 이미지 결과의 품질, 권리, 브랜드 적합성을 ShopSource가 보증하지 않습니다.
- 텍스트 포함 여부는 OCR로 추측해 통과시키지 않습니다. precomposed text 이미지의 mobile crop 및 접근성을 사람이 확인해야 합니다.
- 기술 검사는 visual quality, 저작권, Shopify theme 적합성을 보증하지 않습니다.
- 이 단계 구현과 테스트 중 Shopify, Amazon, Keepa, Spark, 유료 생성 API를 실행하지 않았습니다.
