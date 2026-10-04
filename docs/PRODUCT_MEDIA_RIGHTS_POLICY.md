# 상품 미디어 권리 정책

## 권리 상태

- `SUPPLIER_AUTHORIZED`: 공급자가 사용 허가를 명시적으로 제공
- `MERCHANT_OWNED`: 판매자가 직접 소유/촬영
- `LICENSED`: 사용 범위가 확인된 라이선스 보유
- `GENERATED_LIFESTYLE_ONLY`: 라이프스타일/분위기 이미지에만 사용 가능
- `MANUAL_REVIEW_REQUIRED`: 사람이 권리와 대상 일치를 확인해야 함
- `NO_RIGHTS_CONFIRMED`: 권리가 확인되지 않음

Amazon 상품 상세 페이지의 사진을 볼 수 있다는 사실은 재사용 허가가 아닙니다. 사용 권리가 확인되지 않은 exact product image는 ACTIVE로 공개하지 마세요. 필요하다면 DRAFT 상태로 검토할 수 있지만, 승인 전에는 이미지 없는/수동 검토 상태로 표시해야 합니다.

생성형 이미지는 실제 제품을 촬영한 것처럼 오해시키면 안 됩니다. 생성형 generic lifestyle는 hero 및 컬렉션 장면에 사용할 수 있지만 상품의 정확한 형태·구성·색상·크기를 증명하는 사진 대체품으로 분류하지 않습니다.

각 자산에 대상, 권리 상태, 검토자 확인, alt text, 해상도, 깨진 링크/워터마크 여부 및 승인 기록을 저장하세요. 불확실하면 `MANUAL_REVIEW_REQUIRED` 또는 `NO_RIGHTS_CONFIRMED`로 둡니다.
