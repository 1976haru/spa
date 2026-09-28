# Codex Phase 1 prompt

`D:\shop\shop-source-studio` 저장소 전체를 먼저 읽어라.
특히 `AGENTS.md`, `docs/MASTER_SPEC.md`, `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/SPARK_INTEGRATION.md`, `docs/ROADMAP.md`를 우선 읽어라.

현재 v0.1 초안이 이미 구현되어 있다. 새로 갈아엎지 말고 **감사 + 안정화**부터 한다.

목표:
1. SQLite schema와 Spark storage importer가 10만~50만 occurrence까지 견딜 수 있는지 검토한다.
2. `storage.zip`/폴더 import가 idempotent한지 확인한다.
3. ASIN 중복제거와 occurrence 보존이 정확한지 확인한다.
4. Store Profile 가격구간이 코드 하드코딩 없이 수정 가능한지 확인한다.
5. $35 상품 등이 삭제되지 않고 RESERVE로 남는지 테스트한다.
6. 위험상품이 삭제되지 않고 REVIEW/RESTRICTED + reason으로 남는지 확인한다.
7. manual override가 재분류 후 유지되는지 테스트를 추가한다.
8. Windows 경로/한글 경로/ZIP 경로 테스트를 추가한다.
9. GUI가 DB/Rule Engine을 직접 우회하지 않는지 확인한다.
10. Spark Center는 연동계약 미확정이므로 쓰기 연동을 구현하지 마라. 대신 connector interface/TODO만 검토한다.

필수 검증:
- `python -m compileall -q src tests`
- `pytest -q`
- `git diff --check`

이번 Phase에서 직접 Amazon crawler, CAPTCHA 우회, Spark Center 비공식 API 호출은 만들지 마라.

완료 시:
- 발견한 문제
- 수정한 파일
- 테스트 결과
- v0.2로 넘길 항목
을 보고하라.
