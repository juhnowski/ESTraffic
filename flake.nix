{
  description = "Стенд для сбора сырых сегментов Apache Lucene из Elasticsearch / OpenSearch";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux"; # Измените на aarch64-linux / x86_64-darwin, если у вас другая платформа
      pkgs = import nixpkgs { inherit system; };
      
      pythonEnv = pkgs.python3.withPackages (ps: [
        ps.opensearch-py # Официальный питоновский клиент, совместимый с ES API
      ]);
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        buildInputs = [
          pkgs.opensearch
          pkgs.jre
          pythonEnv
        ];

        shellHook = ''
          export ES_DIR="$PWD/opensearch_data"
          export ES_CONF="$ES_DIR/config"
          export PORT=9200
          
          mkdir -p "$ES_CONF" "$ES_DIR/data" "$ES_DIR/logs"

          # Генерируем минимальный opensearch.yml
          if [ ! -f "$ES_CONF/opensearch.yml" ]; then
            echo "[Nix] Создание локальной конфигурации OpenSearch..."
            cat <<EOF > "$ES_CONF/opensearch.yml"
cluster.name: test-cluster
node.name: test-node-1
path.data: $ES_DIR/data
path.logs: $ES_DIR/logs
network.host: 127.0.0.1
http.port: $PORT
discovery.type: single-node

# Отключаем плагин безопасности для простоты локального тестирования без SSL/паролей
plugins.security.disabled: true
EOF
          fi

          export OPENSEARCH_PATH_CONF="$ES_CONF"

          echo "--------------------------------------------------------"
          echo " Доступные команды Elasticsearch-стенда:"
          echo "   start-es   - Запустить локальный инстанс"
          echo "   stop-es    - Остановить инстанс"
          echo "   run-bench  - Сгенерировать поисковый индекс и собрать сегменты"
          echo "--------------------------------------------------------"

          alias start-es="opensearch -d -p \$ES_DIR/opensearch.pid"
          alias stop-es="kill \$(cat \$ES_DIR/opensearch.pid) && rm \$ES_DIR/opensearch.pid"
          alias run-bench="python collect_es_blocks.py"
        '';
      };
    };
}
