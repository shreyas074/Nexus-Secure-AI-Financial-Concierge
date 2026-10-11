"""Build the local vector index.   python build_index.py"""
import os

from knowledge_base import KnowledgeIndex, chroma_path

if __name__ == "__main__":
    index = KnowledgeIndex(rebuild=True)
    print(f"backend={index.backend_name} chunks={index.chunk_count}")
    if index.backend_name == "chroma":
        print("persisted to", os.path.abspath(chroma_path()))
    for hit in index.search("can I pay my insurance premium by auto-debit", k=3):
        print(f"  {hit['score']:.3f}  {hit['chunk_id']}  ({hit['kind']})")
