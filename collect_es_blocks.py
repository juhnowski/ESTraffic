import os
import shutil
import time
import subprocess
from opensearchpy import OpenSearch

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
    
    # Останавливаем сервис для безопасного копирования файлов
    print("\n[+] Останавливаем OpenSearch для фиксации файлов сегментов...")
    subprocess.run("kill $(cat ./opensearch_data/opensearch.pid) && rm ./opensearch_data/opensearch.pid", shell=True)
    time.sleep(3)
    
    print("\n[+] Сбор бинарных файлов данных Apache Lucene (.fdt / .cfs)...")
    for name, uuid in index_mapping_paths.items():
        # Путь к сегментам первого шарда: index_uuid/0/index/
        shard_dir = os.path.join(ES_DATA_DIR, uuid, "0", "index")
        
        if not os.path.exists(shard_dir):
            print(f"    Ошибка: Директория {shard_dir} не найдена!")
            continue
            
        # Современный Lucene может упаковывать сегменты в составной файл .cfs (Compound File)
        # или хранить раздельно в .fdt. Ищем файлы крупнее пары килобайт.
        target_files = [f for f in os.listdir(shard_dir) if f.endswith(".cfs") or f.endswith(".fdt")]
        
        if not target_files:
            print(f"    Ошибка: Файлы данных сегментов в {name} не найдены!")
            continue
            
        # Берем самый большой файл (это и есть наш объединенный сегмент с документами)
        target_files.sort(key=lambda x: os.path.getsize(os.path.join(shard_dir, x)), reverse=True)
        largest_file = target_files[0]
        
        src_file = os.path.join(shard_dir, largest_file)
        ext = largest_file.split(".")[-1]
        dest_file = os.path.join(OUTPUT_DIR, f"elasticsearch_{name}_segment.{ext}.raw")
        
        shutil.copy(src_file, dest_file)
        size_kb = os.path.getsize(dest_file) // 1024
        print(f"    Сохранено: {dest_file} ({size_kb} KB)")
        
    print("\n[+] Сбор блоков для Elasticsearch/OpenSearch завершен!")

if __name__ == "__main__":
    main()
