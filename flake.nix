{
  description = "Стенд для сбора сырых сегментов Apache Lucene из Elasticsearch / OpenSearch";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux"; 
      pkgs = import nixpkgs { inherit system; };
      
      pythonEnv = pkgs.python3.withPackages (ps: [
        ps.opensearch-py
      ]);
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        buildInputs = [
          pkgs.cargo
          pkgs.rustc
          pkgs.jre
          pythonEnv
          pkgs.gnutar
          pkgs.gzip
          pkgs.curl
        ];

        shellHook = ''
          export ES_DIR="$PWD/opensearch_runtime"
          export PORT=9200
          
          # Если папка не создана или пуста — качаем и настраиваем
          if [ ! -f "$ES_DIR/bin/opensearch" ]; then
            echo "[Nix] Очистка старых битых файлов..."
            rm -rf "$ES_DIR" opensearch_download.tar.gz
            mkdir -p "$ES_DIR"

            echo "[Nix] Скачивание официального релиза OpenSearch 2.11.0 (с защитой от сбоев)..."
            # curl с флагами -L (follow redirect), -C - (resume), --retry (повторы при сбоях)
            curl -L --retry 5 --retry-delay 3 -C - \
              "https://artifacts.opensearch.org/releases/bundle/opensearch/2.11.0/opensearch-2.11.0-linux-x64.tar.gz" \
              -o opensearch_download.tar.gz

            echo "[Nix] Распаковка архива..."
            tar -xzf opensearch_download.tar.gz -C "$ES_DIR" --strip-components=1
            rm opensearch_download.tar.gz
            
            echo "[Nix] Конфигурация локального инстанса..."
            cat <<EOF > "$ES_DIR/config/opensearch.yml"
cluster.name: test-cluster
node.name: test-node-1
path.data: $ES_DIR/data
path.logs: $ES_DIR/logs
network.host: 127.0.0.1
http.port: $PORT
discovery.type: single-node
plugins.security.disabled: true
EOF

            cat <<EOF > "$ES_DIR/config/jvm.options.d/memory.options"
-Xms512m
-Xmx512m
EOF
          fi

          echo "--------------------------------------------------------"
          echo " Доступные команды Elasticsearch-стенда:"
          echo "   start-es   - Запустить локальный инстанс"
          echo "   stop-es    - Остановить инстанс"
          echo "   run-bench  - Сгенерировать поисковый индекс и собрать сегменты"
          echo "   run-entropy - Вычислить энтропию"
          echo "--------------------------------------------------------"

          alias start-es="$ES_DIR/bin/opensearch -d -p \$ES_DIR/opensearch.pid"
          alias stop-es="[ -f \$ES_DIR/opensearch.pid ] && kill \$(cat \$ES_DIR/opensearch.pid) && rm \$ES_DIR/opensearch.pid"
          alias run-bench="python collect_es_blocks.py"
          alias run-entropy="cargo run --release --manifest-path=$PWD/entropy_analyzer/Cargo.toml --"
        '';
      };
    };
}
