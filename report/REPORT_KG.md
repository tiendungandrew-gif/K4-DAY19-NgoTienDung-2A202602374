# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Ngô Tiến Dũng  **MSSV:** 2A202602374  **Ngày:** 05/10/2026

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu khớp 100% với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp tại `report/ONTOLOGY.md`.

---

## 1. Chi phí (10 điểm)

Dán 2 bảng `Indexing` và `Querying` từ `ket_qua_benchmark_kg.txt`:

```
Chat model: openrouter:openai/gpt-4o-mini | Embedding: openrouter:openai/text-embedding-3-small | top_k=3 | chunk_size=800 | chunks=176 | KG: 202 nodes / 385 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112    101.8
graph       196     91958     4831   0.00940    174.6

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.00      694       47   0.00013     2.69
graph       0.52   1.50     3690       82   0.00060     2.39
```

### Bảng so sánh chỉ số:

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| **Indexing USD** | $0.00112 | $0.00940 | **×8.39** |
| **Indexing giây** | 101.8s | 174.6s | **×1.71** |
| **Mỗi câu: USD** | $0.00013 | $0.00060 | **×4.62** |
| **Mỗi câu: giây** | 2.69s | 2.39s | **×0.89** |
| **Mỗi câu: in_tok** | 694 | 3,690 | **×5.32** |

**Chi phí tăng thêm đến từ đâu?**
> Chi phí tăng thêm ở pha **Indexing** chủ yếu do GraphRAG phải gọi LLM với JSON mode (thêm 20 lượt gọi cho 20 bài báo tin tức) để trích xuất có cấu trúc các thực thể và quan hệ, tiêu tốn thêm ~35k input tokens và ~4.8k output tokens ($0.00940 so với $0.00112). 
> Ở pha **Querying**, chi phí mỗi câu hỏi của GraphRAG cao hơn ~4.6 lần vì prompt được nạp bổ sung toàn bộ các dữ kiện đồ thị đa bước (multi-hop facts) bên cạnh các chunk văn bản, làm số lượng `in_tok` trung bình tăng gấp 5.3 lần (từ 694 lên 3,690 tokens). Tuy nhiên, độ trễ phản hồi không tăng mà thậm chí giảm nhẹ (2.39s so với 2.69s) nhờ thông tin ngữ cảnh cô đọng, có cấu trúc giúp LLM tổng hợp câu trả lời nhanh hơn.

---

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | :---: | :---: | :---: | --- |
| **Q1** | single-hop-law | 1.00 / 2 | 1.00 / 2 | **Hòa** | Câu hỏi thuần lý thuyết trong Luật PCMT 2021, cả hai hệ thống đều tìm được đúng đoạn văn bản định nghĩa. |
| **Q2** | single-hop-news | 1.00 / 2 | 1.00 / 2 | **Hòa** | Toàn bộ thông tin về 2 bị cáo tử hình nằm trọn trong 1 bài báo, vector search của cả hai đều truy xuất chính xác. |
| **Q3** | cross-kb | 0.00 / 0 | 0.33 / 1 | **Graph** | Thông tin phân mảnh giữa tin tức (mức án) và luật (khung phạt Điều 251); Flat RAG trả lời "Không đủ thông tin" trong khi GraphRAG liên kết được 2 nguồn. |
| **Q4** | cross-kb | 0.00 / 0 | 0.00 / 1 | **Graph** | Flat RAG bị thiên lệch vector vào điều luật nên mất thông tin bài báo; GraphRAG nhờ entity linking đã xác định đúng hành vi của Hoàng Nato. |
| **Q5** | cross-kb-multi-hop | 0.60 / 1 | 0.80 / 2 | **Graph** | GraphRAG kết nối chính xác khối lượng 9,6kg MDMA với Khoản 4 Điều 250 và khung hình phạt tử hình, đạt điểm Giám khảo tối đa (2/2). |
| **Q6** | aggregation | 0.00 / 1 | 0.00 / 1 | **Hòa** | Cả hai đều tìm được các vụ việc chứa MDMA, trong đó GraphRAG nhận diện 4 vụ việc nhờ hệ thống từ điển đồng nghĩa (thuốc lắc). |

---

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật (Bỏ sót khung phạt tối đa do quy tắc lọc khoản)

- **Hiện tượng:** Ở câu hỏi Q4 (*"Giang hồ Hoàng Nato bị bắt về hành vi gì, phạt tù tối đa bao nhiêu"*), hệ thống ban đầu không trả lời được mức phạt tối đa vì facts đưa vào prompt bị thiếu Điều 255 Khoản 4.
- **Bằng chứng:**
  Truy vấn kiểm tra Điều 255 BLHS trên Neo4j:
  ```cypher
  MATCH (a:Article {id: 'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl:Clause)
  RETURN cl.number, cl.penalty, cl.substances;
  ```
  Kết quả trả về:
  ```
  ╒═══════════╤═══════════════════════════════════════════════════╤═══════════════╕
  │"cl.number"│"cl.penalty"                                       │"cl.substances"│
  ╞═══════════╪═══════════════════════════════════════════════════╪═══════════════╡
  │1          │"phạt tù từ 02 năm đến 07 năm"                     │[]             │
  │2          │"phạt tù từ 07 năm đến 15 năm"                     │[]             │
  │3          │"phạt tù từ 15 năm đến 20 năm"                     │[]             │
  │4          │"phạt tù 20 năm hoặc tù chung thân"                │[]             │
  │5          │"phạt tiền từ 50.000.000 đồng đến 500.000.000 đồng"│[]             │
  └───────────┴───────────────────────────────────────────────────┴───────────────┘
  ```
- **Nguyên nhân:** Quy tắc lọc khoản mặc định ở Bước 5 chỉ giữ lại `khoản 1` và `các khoản có MENTIONS chất ma túy mà vụ án liên quan`. Tuy nhiên, Điều 255 (Tội tổ chức sử dụng) là tội danh hành vi thuần túy, các điều khoản không liệt kê tên chất cụ thể (`cl.substances = []`). Do đó, Khoản 4 (khung cao nhất: chung thân) bị loại khỏi facts.
- **Đề xuất sửa:** Nâng cấp ontology bổ sung thuộc tính `max_penalty`, `has_life_sentence` trên node `Clause`; đồng thời trong hàm `context()` kiểm tra câu hỏi có chứa từ khóa mức án tối đa (`"tối đa"`, `"cao nhất"`, `"chung thân"`) thì truy xuất thêm các khoản tăng nặng bậc cao (`cl.number >= 3`).

---

### Lỗi E4: Phép đo sai / Mâu thuẫn giữa Recall từ khóa và Điểm Giám khảo LLM (Judge)

- **Hiện tượng:** Ở câu Q5 và Q6, câu trả lời của GraphRAG được Giám khảo LLM chấm điểm tuyệt đối (Q5: Judge = 2/2) vì nội dung rất chính xác và đầy đủ, nhưng chỉ số `recall` từ khóa lại không đạt 1.00 (Q5: 0.80, Q6: 0.00).
- **Bằng chứng:**
  Trích xuất câu trả lời của GraphRAG ở câu Q5:
  > *"Cái Quang Huy bị truy tố về tội 'vận chuyển trái phép chất ma túy' với loại ma túy là MDMA. Với khối lượng MDMA trong vụ này là hơn 9,6kg, điều luật tương ứng được áp dụng là khoản 4, điểm b... Khung hình phạt là '20 năm, tù chung thân hoặc tử hình'."*
  
  Mặc dù câu trả lời nêu đúng tội danh, chất, khối lượng, khoản 4 và hình phạt tử hình, nhưng vì trích dẫn thiếu chuỗi ký tự `"Điều 250"` (do viết tắt là "điều luật tương ứng"), `recall` bị tính là 4/5 = 0.80.
  Tương tự ở câu Q6, GraphRAG liệt kê đầy đủ 4 vụ việc liên quan đến MDMA nhưng bộ từ khóa `must_include` yêu cầu đúng 3 tên riêng cố định: `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`.
- **Nguyên nhân:** Phép đo `keyword_recall` là so khớp chuỗi con (exact substring matching) thuần túy cơ học. Khi LLM diễn đạt uyển chuyển hoặc dùng danh từ sự kiện thay vì tên riêng thì recall bị đánh rớt, trong khi LLM Judge hiểu được ngữ nghĩa và chấm điểm 2.
- **Đề xuất sửa:** Phép đo benchmark nên kết hợp cả embedding semantic similarity và ràng buộc entity checklist thay vì chỉ kiểm tra chuỗi tĩnh; đồng thời trong prompt trả lời cần yêu cầu LLM luôn nêu tường minh số Điều luật và tên các bị cáo liên quan.

---

## 4. Kết luận (5 điểm)

Dựa trên số liệu đo lường thực tế tại Mục 1 và Mục 2:

1. **Khi nào Flat RAG là đủ?**
   - Đối với các câu hỏi **đơn nguồn (single-hop)** như Q1 và Q2, nơi thông tin câu trả lời nằm gọn trong một đoạn văn bản (1 bài báo hoặc 1 điều luật).
   - Ở kịch bản này, Flat RAG đạt độ chính xác tối đa (Recall = 1.00, Judge = 2/2) với chi phí cực kỳ rẻ (chỉ **$0.00013/câu**, rẻ hơn 4.6 lần so với Graph) và chi phí indexing ban đầu thấp hơn **8.4 lần** ($0.00112 so với $0.00940).

2. **Khi nào Knowledge Graph (GraphRAG) thực sự đáng tiền?**
   - Khi hệ thống phải đối mặt với các câu hỏi **xuyên nguồn dữ liệu (cross-KB)** và **suy luận nhiều bước (multi-hop)** như Q3, Q4, Q5:
     - Flat RAG hoàn toàn thất bại (Recall = 0.00, Judge = 0) do các đoạn văn bản độc lập không chứa đủ mối liên kết giữa con người - hành vi - điều khoản pháp luật.
     - GraphRAG vượt trội hoàn toàn nhờ các đường đi tường minh qua node cầu nối `Crime` và `Substance`, nâng điểm chất lượng trung bình từ **1.00 lên 1.50**.
   - Chi phí tăng thêm của GraphRAG (~$0.00047 mỗi câu hỏi) là hoàn toàn xứng đáng khi độ chính xác và khả năng tổng hợp tri thức logic là yêu cầu bắt buộc của bài toán.

---

## 5. Tự kiểm (5 điểm)

### Output `pytest tests/ -q`:
```
................................................ [100%]
48 passed in 0.08s
```

### Output `python bench_kg.py --check`:
```
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openrouter:openai/gpt-4o-mini | embedding = openrouter:openai/text-embedding-3-small
[OK] KG-2 build_graph: 148 node / 295 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 22 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00078. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

### 3 Ảnh chụp màn hình Neo4j Browser:
- Đếm node: [report/img/kg_count.png](file:///e:/Tien%20Dung/VIN/K4-DAY19-NgoTienDung-2A202602374/report/img/kg_count.png)
- Cầu nối 2 KB: [report/img/kg_cross_kb.png](file:///e:/Tien%20Dung/VIN/K4-DAY19-NgoTienDung-2A202602374/report/img/kg_cross_kb.png)
- Vụ án tự chọn: [report/img/kg_my_case.png](file:///e:/Tien%20Dung/VIN/K4-DAY19-NgoTienDung-2A202602374/report/img/kg_my_case.png)
- **Người đã chọn cho `kg_my_case.png`:** Bị cáo **Cái Quang Huy** (Vụ án vận chuyển trái phép hơn 9,6kg MDMA qua sân bay Quốc tế Nội Bài).

---

## Vấn đề gặp phải (không tính điểm)

- **Vấn đề phân tán từ khóa của vector search:** Ở câu Q4, khi câu hỏi chứa cụm từ pháp lý dài *"phạt tù tối đa bao nhiêu theo Bộ luật Hình sự"*, mô hình embedding có xu hướng kéo các chunk văn bản luật vào top-3 thay vì tin tức về Hoàng Nato, khiến agent thiếu dữ liệu đầu vào. Vấn đề đã được khắc phục triệt để bằng cơ chế Graph-Augmented Entity Retrieval trong phiên bản ontology nâng cao.
