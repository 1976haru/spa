# Store Profile

예시: Cabin Tidy

```json
{
  "store_id": "001",
  "store_name": "Cabin Tidy",
  "category": "auto_interior",
  "price_bands": [
    {"name":"under_30","min":0,"max":30,"status":"LOW_RESERVE"},
    {"name":"30_to_35","min":30,"max":35,"status":"RESERVE_C"},
    {"name":"35_to_40","min":35,"max":40,"status":"RESERVE_B"},
    {"name":"40_to_100","min":40,"max":100,"status":"PRIMARY"},
    {"name":"100_to_120","min":100,"max":120,"status":"RESERVE_A"},
    {"name":"120_plus","min":120,"max":null,"status":"HIGH_RESERVE"}
  ]
}
```

가격 경계는 `min <= price < max`를 사용한다. 가격 구간은 코드 수정 없이 JSON에서 바꾼다.
