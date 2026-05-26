use clap::Parser;
use comfy_table::{Table, Cell, Color};
use indicatif::{ProgressBar, ProgressStyle};
use std::fs::{self, File};
use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

#[derive(Parser, Debug)]
#[command(author, version, about = "Глубокий пакетный анализ энтропии Шеннона для сырых данных")]
struct Args {
    /// Путь к каталогу с .raw файлами
    #[arg(short, long, default_value = "collected_lucene_segments/")]
    dir: String,

    /// Размер скользящего окна в байтах для локального анализа
    #[arg(short, long, default_value_t = 4096)]
    window_size: usize,
}

struct EntropyResult {
    file_name: String,
    global_entropy: f64,
    byte_counts: [u64; 256],
    total_bytes: u64,
    min_local_entropy: f64,
    max_local_entropy: f64,
}

fn main() -> std::io::Result<()> {
    let args = Args::parse();
    let dir_path = Path::new(&args.dir);

    if !dir_path.exists() || !dir_path.is_dir() {
        eprintln!("Ошибка: Директория '{}' не найдена или не является каталогом.", args.dir);
        std::process::exit(1);
    }

    println!("[+] Сканирование каталога: {}", dir_path.display());
    println!("[+] Размер локального окна: {} байт\n", args.window_size);

    // Собираем абсолютно все файлы в папке (любые расширения)
    let mut files: Vec<PathBuf> = fs::read_dir(dir_path)?
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path())
        .filter(|path| path.is_file()) // Оставляем только файлы, игнорируем подпапки
        .collect();


    // Сортируем файлы по имени для красивого вывода
    files.sort();

    if files.is_empty() {
        println!("[-] В каталоге не найдено файлов с расширением .raw");
        return Ok(());
    }

    let mut reports = Vec::new();

    // Анализируем каждый файл
    for file_path in files {
        let file_name = file_path.file_name().unwrap().to_string_lossy().into_owned();
        let file = File::open(&file_path)?;
        let total_bytes = file.metadata()?.len();

        if total_bytes == 0 {
            println!("[-] Пропуск пустого файла: {file_name}");
            continue;
        }

        println!("[*] Анализ файла: {file_name}");
        let result = analyze_file(file, total_bytes, args.window_size, file_name)?;
        reports.push(result);
        println!();
    }

    // Выводим финальную сводную таблицу
    print_summary_table(&reports);

    Ok(())
}

fn analyze_file(file: File, total_bytes: u64, window_size: usize, file_name: String) -> std::io::Result<EntropyResult> {
    let mut reader = BufReader::new(file);
    let mut byte_counts = [0u64; 256];
    let mut buffer = vec![0u8; 128 * 1024]; // 128KB буфер для скорости

    let pb = ProgressBar::new(total_bytes);
    pb.set_style(
        ProgressStyle::with_template("[{elapsed_precise}] [{bar:40.cyan/blue}] {bytes}/{total_bytes} ({eta})")
            .unwrap()
            .progress_chars("#>-")
    );

    loop {
        let bytes_read = reader.read(&mut buffer)?;
        if bytes_read == 0 {
            break;
        }
        for &byte in &buffer[..bytes_read] {
            byte_counts[byte as usize] += 1;
        }
        pb.inc(bytes_read as u64);
    }
    pb.finish_and_clear(); // Очищаем бар, чтобы не спамить в терминал

    // Глобальная энтропия
    let mut global_entropy = 0.0;
    for &count in byte_counts.iter() {
        if count > 0 {
            let p = count as f64 / total_bytes as f64;
            global_entropy -= p * p.log2();
        }
    }

    // Локальная энтропия
    let mut file = reader.into_inner();
    file.seek(SeekFrom::Start(0))?;
    let mut reader = BufReader::new(file);

    let mut min_local_entropy = 8.0;
    let mut max_local_entropy = 0.0;
    let mut window_buffer = vec![0u8; window_size];
    
    loop {
        let bytes_read = reader.read(&mut window_buffer)?;
        if bytes_read == 0 {
            break;
        }
        
        let current_window = &window_buffer[..bytes_read];
        let local_ent = calculate_buffer_entropy(current_window);
        
        if local_ent < min_local_entropy { min_local_entropy = local_ent; }
        if local_ent > max_local_entropy { max_local_entropy = local_ent; }
        
        if bytes_read < window_size {
            break;
        }
    }

    Ok(EntropyResult {
        file_name,
        global_entropy,
        byte_counts,
        total_bytes,
        min_local_entropy,
        max_local_entropy,
    })
}

fn calculate_buffer_entropy(buffer: &[u8]) -> f64 {
    if buffer.is_empty() { return 0.0; }
    let mut counts = [0u32; 256];
    for &b in buffer {
        counts[b as usize] += 1;
    }
    let len = buffer.len() as f64;
    let mut entropy = 0.0;
    for &count in counts.iter() {
        if count > 0 {
            let p = count as f64 / len;
            entropy -= p * p.log2();
        }
    }
    entropy
}

fn print_summary_table(reports: &[EntropyResult]) {
    println!("=== СВОДНЫЙ СРАВНИТЕЛЬНЫЙ АНАЛИЗ ДАТАСЕТОВ ===");
    
    let mut table = Table::new();
    table.set_header(vec![
        "Имя файла", 
        "Размер", 
        "Гл. энтропия", 
        "Мин. лок. энт.", 
        "Макс. лок. энт.",
        "Потенциал дедупликации"
    ]);
    
    for r in reports {
        let size_mb = r.total_bytes as f64 / 1024.0 / 1024.0;
        
        // Автоматическая оценка потенциала сжатия цветом
        let (verdict, color) = match r.global_entropy {
            e if e < 2.0 => ("Идеальный (Высокий)", Color::Green),
            e if e < 5.0 => ("Хороший", Color::Cyan),
            e if e < 7.0 => ("Средний (Нужен CDC)", Color::Yellow),
            _ => ("Нулевой (Шум/Сжато)", Color::Red),
        };

        table.add_row(vec![
            Cell::new(&r.file_name),
            Cell::new(format!("{size_mb:.2} MB")),
            Cell::new(format!("{:.4} бит", r.global_entropy)),
            Cell::new(format!("{:.4} бит", r.min_local_entropy)),
            Cell::new(format!("{:.4} бит", r.max_local_entropy)),
            Cell::new(verdict).fg(color),
        ]);
    }
    
    println!("{table}");
}
