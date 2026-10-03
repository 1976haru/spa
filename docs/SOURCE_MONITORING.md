# Source Monitoring

Source monitoring은 snapshot 이력을 추가하고 변화에 대한 action plan만 만듭니다. 기본 동작은 `PREVIEW_ONLY / ALERT_ONLY`이며 숨은 daemon을 설치하지 않습니다. 필요하면 Windows Task Scheduler가 다음 one-shot 명령을 호출할 수 있습니다.

```powershell
python -m shopsource.cli source-monitor --store 001 --due-only
```

우선순위는 Shopify mapped 상품, 가격 경고 상품, 품절 재확인, reserve 순입니다. Provider 오류는 품절로 바꾸지 않고 마지막 성공 snapshot과 freshness를 사용합니다. 오래된 데이터는 결국 신규 등록을 차단하고 기존 상품에는 PAUSE_LISTING 제안만 만듭니다.

재입고는 한 번의 신호로 자동 복구하지 않습니다. 기본적으로 두 번 연속 IN_STOCK, FRESH, margin 비차단이어야 `RESTORE_ELIGIBLE`입니다. merchant가 직접 끈 상품은 ShopSource가 다시 켜지 않습니다.

Inventory ownership은 `UNMANAGED`, `ALERT_ONLY`, `SHOP_SOURCE_MANAGED`로 구분합니다. 기본 UNMANAGED는 Amazon 재고와 자동 동기화된다는 뜻이 아닙니다. 향후 inventory 변경도 명시적 소유권·scope·location·확인 없이는 실행할 수 없습니다.

