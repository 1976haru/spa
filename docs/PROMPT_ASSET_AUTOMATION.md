# 이미지 프롬프트 자동화와 수동 업로드

ShopSource는 스토어·브랜드·컬렉션·홈페이지 계획을 이용해 Hero 배너, 컬렉션 이미지, 카테고리 카드, 소개/헤더 보조 이미지용 프롬프트를 만듭니다. 기본 동작은 로컬에서 프롬프트를 만들고 내보내는 것이며, 유료 이미지 API를 호출하지 않습니다.

## 초보자 사용 순서

1. `001 | Cabin Tidy`를 선택하고 홈페이지를 자동 설계합니다.
2. `프롬프트 자동 생성`을 눌러 필요한 이미지 프롬프트를 준비합니다.
3. Hero 또는 원하는 컬렉션/카테고리 프롬프트를 열고 `복사`합니다. Hero 이미지는 글자 없는 배경 이미지로 만들고, 제목과 버튼은 Shopify 테마의 텍스트 오버레이로 따로 두는 것이 기본입니다.
4. 복사한 내용을 ChatGPT 등 외부 이미지 도구에 붙여넣고 이미지를 생성해 로컬에 저장합니다.
5. 기존 Hero/컬렉션 이미지 업로드 화면에서 파일을 선택해 검사합니다. 해상도·비율 경고를 확인하고 직접 승인해야 적용 미리보기에서 사용할 수 있습니다.
6. 홈페이지 미리보기를 새로 만든 다음 링크·컬렉션 대상과 desktop/mobile 구도를 검토합니다. Shopify 변경은 별도 미리보기와 사람의 확인 없이는 실행되지 않습니다.

전체 프롬프트를 내보내면 `exports/asset_prompts/<store>/<run_id>/`에 asset별 Markdown, `asset_prompts.json`, 복사하기 쉬운 `copy_paste_bundle.txt`가 생성됩니다.

## 지원 이미지 종류

- 무문자 홈페이지 Hero 배너, 텍스트 포함 대체안, 모바일 crop 안내와 alt 문구
- 개별 collection 이미지 및 해당 실제 collection에 연결할 category shortcut 카드
- 헤더 보조, featured collection, About, Contact/지원 섹션 이미지
- 기존 로고를 바꾸지 않는 헤더 배치·안전 배경·favicon 재사용 안내

프롬프트는 가짜 후기·별점·보증·긴급성, 로고·워터마크, 검증되지 않은 상품 주장을 생성하지 않도록 지시합니다. 생성 이미지의 권리와 사실성은 사용자가 확인해야 합니다.

## 선택형 YouTubeSum 연결

ShopSource는 `D:\03_youtubesum\shopsource_bridge.py`를 subprocess와 버전이 지정된 JSON job/result 계약으로 호출하며 Python 패키지를 직접 import하지 않습니다. 저장소/bridge/생성기가 없거나 생성 모델이 구성되지 않았으면 `PROMPT_ONLY_FALLBACK` 또는 `WAITING_FOR_CONFIGURATION`을 표시합니다. 이 환경의 YouTubeSum 저장소에는 headless bridge와 이미지 처리 기능이 있지만 준비된 생성 모델이 없어 실제 생성기는 아직 READY가 아닙니다.

필요하면 다음 환경변수로 경로와 실행기를 지정할 수 있습니다: `SHOP_SOURCE_YOUTUBESUM_PATH`, `SHOP_SOURCE_YOUTUBESUM_PYTHON`, `SHOP_SOURCE_YOUTUBESUM_BRIDGE`, `SHOP_SOURCE_IMAGE_OUTPUT`, `SHOP_SOURCE_IMAGE_TIMEOUT`, `SHOP_SOURCE_IMAGE_CANDIDATES`. 실행은 AUTO queue에 저장되며 후보 검사 후 사용자가 선택·승인해야 ShopSource 자산으로 등록됩니다. 승인된 자산 등록은 Shopify 업로드/테마 적용과 별개입니다.

## Pillow와 이미지 검사

Pillow는 프로젝트의 기본 Python 의존성(`Pillow>=10`)입니다. UI에 `No module named PIL`이 나오면 UI를 실행하는 동일한 `.venv`에서 `python -m pip install -e ".[ui]"`를 실행하고 재시작하세요. 시작 배치 파일과 UI의 환경 진단/복구 버튼은 현재 실행 interpreter를 확인해 안내합니다.

파일 검사는 존재 여부, 지원 확장자, 실제 디코딩, pixel 크기, 권장 aspect ratio, 투명 배경을 봅니다. 이미지 안의 글자를 일반 OCR 수준으로 판정하지 않으므로 text-free 여부, mobile crop, alt 접근성은 사람이 확인해야 합니다. 작은 이미지나 다른 비율은 경고로 안내하며 임의로 늘리거나 자르지 않습니다.

## 제한 및 안전

- 이 구현 중 Amazon/Keepa, Shopify, Theme, Spark 또는 유료 생성 API를 실행하지 않았습니다.
- YouTubeSum GUI 자동 클릭은 하지 않습니다. headless 생성기는 명시적으로 설정되어 READY일 때만 사용할 수 있습니다.
- 승인되지 않은 이미지와 stale preview는 적용 미리보기에 사용하지 않습니다.
- 기술 검사는 이미지 품질, 사용권, Shopify 테마 호환성 또는 WCAG 적합성을 보증하지 않습니다.
