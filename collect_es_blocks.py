import os
import shutil
import time
import subprocess
from opensearchpy import OpenSearch
import glob

OUTPUT_DIR = "./es_bench_blocks"
ES_DATA_DIR = "./opensearch_data/data/nodes/0/indices"

SCENARIOS = {
    "1_duplicates": {
        "index": "bench_duplicates",
        "data_gen": lambda: [
            {
                "_index": "bench_duplicates",
                "id": i,
                "payload": "КОНСТАНТНЫЙ_ТЕКСТ_ДЛЯ_ПРОВЕРКИ_ДЕДУПЛИКАЦИИ_ВАРИАНТ_Е_Е_Е_Е_Е_Е" if i % 2 == 0 
                           else "ДРУГОЙ_ШАБЛОННЫЙ_БЛОК_ДАННЫХ_МИН_МАКС_Е_Е_Е_Е_Е_Е_Е_Е_Е",
                "repeat_padding": "A" * 1000 # Раздуваем строку, чтобы пробить скользящее окно LZ4
            }
            for i in range(20000)
        ]
    },
    "2_denormalized": {
        "index": "bench_denormalized",
        "data_gen": lambda: [
            {
                "_index": "bench_denormalized",
                "id": i,
                "region": ["Москва", "СПб", "Сибирь"][i % 3],
                "manager": ["Иванов И.И.", "Петров П.П."][i % 2],
                "status": "ACTIVE",
                "score": 100.5 * (i % 10)
            }
            for i in range(25000)
        ]
    },
    "3_binary": {
        "index": "bench_binary",
        "data_gen": lambda: [
            {
                "_index": "bench_binary",
                "id": i,
                # Кодируем случайные байты в HEX, так как ES принимает JSON-совместимые строки
                "raw_hex": os.urandom(2000).hex() 
            }
            for i in range(1000)
        ]
    }
}

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Подключаемся к локальному инстансу
    client = OpenSearch(hosts=[{'host': '127.0.0.1', 'port': 9200}])
    
    # Ждем, пока нода перейдет в статус Green/Yellow после запуска
    print("[+] Ожидание готовности кластера...")
    client.cluster.health(wait_for_status='yellow')
    
    index_mapping_paths = {}
    
    for name, config in SCENARIOS.items():
        idx_name = config["index"]
        
        if client.indices.exists(index=idx_name):
            client.indices.delete(index=idx_name)
            
        # Создаем индекс с базовыми настройками
        client.indices.create(index=idx_name, body={
            "settings": {
                "index": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0
                }
            }
        })
        
        print(f"\n--- Сценарий Elasticsearch: {name} ---")
        print(f"    Индексация документов...")
        
        docs = config["data_gen"]()
        # Пакетная загрузка (bulk)
        bulk_data = ""
        for doc in docs:
            bulk_data += f'{{"index": {{"_index": "{idx_name}", "_id": "{doc["id"]}"}}}}\n'
            # Удаляем системный id из тела самого документа
            body = doc.copy()
            del body["id"]
            del body["_index"]
            import json
            bulk_data += json.dumps(body) + "\n"
            
        client.bulk(body=bulk_data)
        
        # Заставляем Lucene выполнить коммит и записать данные из памяти на диск
        client.indices.refresh(index=idx_name)
        
        # КРИТИЧНО: Принудительно объединяем все мелкие сегменты Lucene в 1 большой файл
        print(f"    Оптимизация сегментов (Force Merge в 1 сегмент)...")
        client.indices.forcemerge(index=idx_name, max_num_segments=1)
        
        # Получаем внутренний UUID индекса, чтобы найти его на диске
        idx_info = client.indices.get(index=idx_name)
        idx_uuid = idx_info[idx_name]["settings"]["index"]["provided_name"]
        
        # На самом деле папка называется по хэшу/UUID, вытащим его через State API
        stats = client.indices.stats(index=idx_name)
        # Получаем реальный UUID директории
        actual_uuid = list(client.indices.get_settings(index=idx_name).keys())[0]
        # Извлекаем метаданные структуры
        index_mapping_paths[name] = actual_uuid

    client.close()
    
    # 1. Исправляем остановку процесса OpenSearch
    PID_PATH = "./opensearch_runtime/opensearch.pid"
    if os.path.exists(PID_PATH):
        print("[+] Останавливаем OpenSearch для фиксации файлов сегментов...")
        with open(PID_PATH, "r") as f:
            pid = f.read().strip()
        try:
            os.kill(int(pid), 15)  # SIGTERM
            print(f"    Процесс {pid} остановлен.")
        except ProcessLookupError:
            pass
        if os.path.exists(PID_PATH):
            os.remove(PID_PATH)

    # 2. Исправляем сбор бинарных файлов Lucene (используем glob для обхода UUID)
    DATA_BASE_DIR = "./opensearch_runtime/data/nodes/0/indices"
    TARGET_DIR = "./collected_lucene_segments"
    os.makedirs(TARGET_DIR, exist_ok=True)

    print("\n[+] Сбор бинарных файлов данных Apache Lucene (.fdt / .cfs)...")

    if not os.path.exists(DATA_BASE_DIR):
        print(f"    Ошибка: Базовая директория данных {DATA_BASE_DIR} не найдена!")
    else:
        # Ищем файлы сегментов во всех подпапках индексов OpenSearch
        # Поиск файлов *.fdt (данные полей) и *.cfs (составные сегменты)
        lucene_files = glob.glob(f"{DATA_BASE_DIR}/**/0/index/*.fdt", recursive=True) + \
                    glob.glob(f"{DATA_BASE_DIR}/**/0/index/*.cfs", recursive=True) + \
                    glob.glob(f"{DATA_BASE_DIR}/**/0/index/*.si", recursive=True)

        if not lucene_files:
            print("    Предупреждение: Файлы Lucene не найдены. Проверьте, завершилась ли запись на диск.")
        else:
            for file_path in lucene_files:
                file_name = os.path.basename(file_path)
                # Чтобы понять, к какому индексу принадлежит файл, можно вытащить UUID из пути
                # Путь обычно: .../indices/[UUID]/0/index/[FILE]
                parts = file_path.split(os.sep)
                uuid_folder = parts[-4] if len(parts) >= 4 else "unknown"
                
                new_name = f"{uuid_folder}_{file_name}"
                shutil.copy2(file_path, os.path.join(TARGET_DIR, new_name))
                print(f"    Скопирован сегмент: {new_name}")

    print("\n[+] Сбор блоков завершен!")

if __name__ == "__main__":
    main()
