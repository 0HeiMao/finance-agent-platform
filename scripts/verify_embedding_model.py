"""离线中文检索烟雾测试，不调用 DeepSeek、不改数据库。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sentence_transformers import SentenceTransformer

MODEL = Path(__file__).resolve().parents[1] / 'data/models/finance-embedding'

def main():
    model = SentenceTransformer(str(MODEL), local_files_only=True)
    documents = ['费用报销须在发生后三十天内提交。', '采购预算需要财务经理审批。', '服务器每晚进行备份。']
    queries = ['报销需要在几天内提交？', '谁负责批准采购预算？']
    vectors = model.encode(documents + queries, normalize_embeddings=True)
    assert vectors.shape[1] == 384, '数据库维度不匹配'
    for index, query in enumerate(queries):
        scores = vectors[:3] @ vectors[3 + index]
        assert int(scores.argmax()) == index, '中文相关性排序失败'
        print('query', index + 1, 'best_document', int(scores.argmax()) + 1, 'cosine_distance', round(1 - float(scores[index]), 4))
    print('PASS: local model, 384 dimensions, two Chinese retrieval examples')

if __name__ == '__main__':
    main()
