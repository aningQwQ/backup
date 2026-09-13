#!/usr/bin/env bash
# migrate-batch.sh
# 水位触发批量迁移：src -> 中转 -> dst
# 多文件并行读入，达到水位后并行写出，然后 sync
# 严格分段：读段只碰源盘，写段只碰目标盘
#
# 默认模拟模式，不写任何文件。真正执行需加 -r。
# 时间统计：读/写时间均包含 sync 落盘，sync 完成后才计为有效。

set -uo pipefail

# ============ 配置区 ============
SRC_ROOT=""
DST_ROOT=""
STAGE_DIR="/tmp"                    # 默认 /tmp，可用 -t 改
LOG_FILE=""                         # 留空 = 写到 STAGE_DIR/migrate.log

WATERMARK_GB=""                     # 必须用 -w 指定
READ_PARALLEL=4
WRITE_PARALLEL=2
SLEEP_AFTER_SYNC=5
DRY_RUN=1                           # 默认模拟，加 -r 才真正执行
# ================================

usage() {
    cat <<'EOF'
用法: ./migrate-batch.sh -s SRC -d DST -w N [选项]

必选:
  -s DIR   源根目录
  -d DIR   目标根目录
  -w N     水位 GB，必须指定

可选:
  -t DIR   中转目录        (默认 /tmp)
  -r       真正执行         (不加则为模拟模式)
  -h       帮助

默认行为:
  不加 -r 时是模拟模式，只扫描、只打印计划，不写任何文件。
  加 -r 后才真正复制。

说明:
  中转目录和大小完全信任用户指定，脚本不做强制校验。
  /tmp 不是 tmpfs 或空间不足时，数据会直接写到该目录所在磁盘。
  大文件超过水位时单独成批，和中转大小无关地尝试，写不下记 [FAIL] 到日志。

时间统计:
  读时间 = 源 -> 中转 cp 完成 + 中转 fsync 落盘
  写时间 = 中转 -> 目标 cp 完成 + 目标 fsync 落盘
  两段时间都包含 sync，sync 完成才计为有效。
EOF
    exit 0
}

while getopts ":s:d:t:w:rh" opt; do
    case $opt in
        s) SRC_ROOT="$OPTARG" ;;
        d) DST_ROOT="$OPTARG" ;;
        t) STAGE_DIR="$OPTARG" ;;
        w) WATERMARK_GB="$OPTARG" ;;
        r) DRY_RUN=0 ;;
        h) usage ;;
        *) echo "未知选项: -$OPTARG" >&2; usage ;;
    esac
done

[ -n "$SRC_ROOT" ]      || { echo "错误: 必须指定 -s 源目录" >&2; usage; }
[ -n "$DST_ROOT" ]      || { echo "错误: 必须指定 -d 目标目录" >&2; usage; }
[ -n "$WATERMARK_GB" ]  || { echo "错误: 必须指定 -w 水位(GB)" >&2; usage; }

case "$WATERMARK_GB" in
    ''|*[!0-9]*) echo "错误: -w 必须是正整数(GB)，收到: $WATERMARK_GB" >&2; exit 1 ;;
esac
[ "$WATERMARK_GB" -gt 0 ] || { echo "错误: -w 必须大于 0" >&2; exit 1; }

# ---------- 日志 ----------
if [ -z "$LOG_FILE" ]; then
    LOG_FILE="${STAGE_DIR%/}/migrate.log"
fi

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"; }
die() { log "[FATAL] $*" >&2; exit 1; }

# ---------- 依赖检查 ----------
for c in findmnt numfmt find stat df cp cmp sync realpath xargs mktemp; do
    command -v "$c" >/dev/null 2>&1 || die "缺少命令: $c"
done

# ---------- 前置检查 ----------
[ -d "$SRC_ROOT" ] || die "源目录不存在: $SRC_ROOT"

if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p -- "$DST_ROOT" "$STAGE_DIR"
fi

SRC_ROOT=$(realpath -m "$SRC_ROOT")
DST_ROOT=$(realpath -m "$DST_ROOT")
STAGE_DIR=$(realpath -m "$STAGE_DIR")

WATERMARK_BYTES=$(( WATERMARK_GB * 1024 * 1024 * 1024 ))

# 中转信息（仅记录，不校验，不阻断）
if [ -d "$STAGE_DIR" ]; then
    TMP_AVAIL=$(df -B1 --output=avail -- "$STAGE_DIR" | tail -1)
    STAGE_FSTYPE=$(findmnt -no FSTYPE --target "$STAGE_DIR" 2>/dev/null || echo "未知")
else
    TMP_AVAIL=0
    STAGE_FSTYPE="不存在(真实执行时会创建)"
fi

log "===== 开始 ====="
log "  模式: $([ "$DRY_RUN" -eq 1 ] && echo '模拟(不写文件)' || echo '真实执行')"
log "  源:   $SRC_ROOT"
log "  目标: $DST_ROOT"
log "  中转: $STAGE_DIR ($STAGE_FSTYPE)"
log "  中转可用: $(numfmt --to=iec-i --suffix=B "$TMP_AVAIL")"
log "  水位: $(numfmt --to=iec-i --suffix=B "$WATERMARK_BYTES")"
log "  并行: 读 $READ_PARALLEL, 写 $WRITE_PARALLEL"
log "  说明: 中转目录和大小信任用户指定，不做强制校验"

# ---------- 路径安全 ----------
safe_rel() {
    local rel="$1"
    case "$rel" in /*) return 1 ;; esac
    local resolved
    resolved=$(realpath -m "$SRC_ROOT/$rel") || return 1
    case "$resolved" in "$SRC_ROOT"/*) return 0 ;; *) return 1 ;; esac
}

# ---------- 批次状态 ----------
declare -a BATCH_RELS=()
BATCH_BYTES=0

reset_batch() { BATCH_RELS=(); BATCH_BYTES=0; }

# ---------- 读取阶段：src -> 中转，并行 ----------
read_phase() {
    local n=${#BATCH_RELS[@]}
    [ "$n" -eq 0 ] && return 0
    log "[P1] 读入中转: $n 个文件, $(numfmt --to=iec-i --suffix=B "$BATCH_BYTES")"

    if [ "$DRY_RUN" -eq 1 ]; then
        local r
        for r in "${BATCH_RELS[@]}"; do
            log "     [DRY-READ] $r"
        done
        return 0
    fi

    printf '%s\0' "${BATCH_RELS[@]}" | \
        xargs -0 -n1 -P "$READ_PARALLEL" bash -c '
            src_root="$1"; stage_dir="$2"; rel="$3"
            src="$src_root/$rel"
            stg="$stage_dir/$rel"
            [ -f "$src" ] || { echo "[SKIP] 源不存在: $rel" >&2; exit 2; }
            mkdir -p -- "$(dirname -- "$stg")"
            rm -f -- "$stg"
            if ! nice -n 19 ionice -c 3 cp --reflink=never \
                    --preserve=mode,timestamps -- "$src" "$stg"; then
                echo "[FAIL] 读取失败: $rel" >&2
                rm -f -- "$stg"
                exit 1
            fi
            s1=$(stat -c%s "$src" 2>/dev/null || echo -1)
            s2=$(stat -c%s "$stg" 2>/dev/null || echo -1)
            if [ "$s1" != "$s2" ]; then
                echo "[FAIL] 读取后大小不一致: $rel" >&2
                rm -f -- "$stg"
                exit 1
            fi
            exit 0
        ' _ "$SRC_ROOT" "$STAGE_DIR"
    return $?
}

# ---------- 写入阶段：中转 -> dst，并行 ----------
write_phase() {
    local n=${#BATCH_RELS[@]}
    [ "$n" -eq 0 ] && return 0
    log "[P2] 写出到目标: $n 个文件"

    if [ "$DRY_RUN" -eq 1 ]; then
        local r
        for r in "${BATCH_RELS[@]}"; do
            log "     [DRY-WRITE] $r"
        done
        return 0
    fi

    printf '%s\0' "${BATCH_RELS[@]}" | \
        xargs -0 -n1 -P "$WRITE_PARALLEL" bash -c '
            stage_dir="$1"; dst_root="$2"; rel="$3"
            stg="$stage_dir/$rel"
            dst="$dst_root/$rel"
            [ -f "$stg" ] || { echo "[SKIP] 中转缺失: $rel" >&2; exit 2; }
            mkdir -p -- "$(dirname -- "$dst")"
            tmp=$(mktemp --tmpdir="$(dirname -- "$dst")" ".migrate.XXXXXX") || exit 1
            if ! nice -n 19 ionice -c 3 cp --reflink=never \
                    --preserve=mode,timestamps -- "$stg" "$tmp"; then
                echo "[FAIL] 写入失败: $rel" >&2
                rm -f -- "$tmp"
                exit 1
            fi
            if ! cmp -s "$stg" "$tmp"; then
                echo "[FAIL] 校验失败: $rel" >&2
                rm -f -- "$tmp"
                exit 1
            fi
            mv -f -- "$tmp" "$dst"
            rm -f -- "$stg"
            exit 0
        ' _ "$STAGE_DIR" "$DST_ROOT"
    return $?
}

# ---------- 整批执行 ----------
# 时间含义：
#   读时间 = read_phase 全部 cp 完成 + sync -f STAGE_DIR 落盘完成
#   写时间 = write_phase 全部 cp 完成 + sync -f DST_ROOT 落盘完成
flush_batch() {
    local n=${#BATCH_RELS[@]}
    [ "$n" -eq 0 ] && return 0

    if [ "$DRY_RUN" -eq 1 ]; then
        log "[BATCH] 本批 $n 个文件, 合计 $(numfmt --to=iec-i --suffix=B "$BATCH_BYTES")"
    fi

    # ===== 阶段一：源 -> 中转，然后 sync 中转 =====
    local t0 t1
    t0=$(date +%s)

    if ! read_phase; then
        log "[WARN] 读入阶段有失败，继续写出已成功的文件"
    fi

    if [ "$DRY_RUN" -eq 0 ]; then
        sync -f "$STAGE_DIR" 2>/dev/null || sync
    fi
    t1=$(date +%s)

    if [ "$DRY_RUN" -eq 0 ]; then
        log "[P1 OK] 读入中转并落盘 $(numfmt --to=iec-i --suffix=B "$BATCH_BYTES") 耗时 $((t1-t0))s"
    else
        log "[P1 OK] (模拟) 耗时 $((t1-t0))s"
    fi

    # ===== 阶段二：中转 -> 目标，然后 sync 目标 =====
    local t2 t3
    t2=$(date +%s)

    if ! write_phase; then
        log "[WARN] 写出阶段有失败"
    fi

    if [ "$DRY_RUN" -eq 0 ]; then
        sync -f "$DST_ROOT" 2>/dev/null || sync
    fi
    t3=$(date +%s)

    if [ "$DRY_RUN" -eq 0 ]; then
        log "[P2 OK] 写出目标并落盘 $(numfmt --to=iec-i --suffix=B "$BATCH_BYTES") 耗时 $((t3-t2))s"
    else
        log "[P2 OK] (模拟) 耗时 $((t3-t2))s"
    fi

    reset_batch
    if [ "$DRY_RUN" -eq 0 ]; then
        sleep "$SLEEP_AFTER_SYNC"
    fi
}

# ---------- 主循环 ----------
main() {
    local n=0 size rel
    local total_files=0
    local total_bytes=0
    local skip_exist=0
    local skip_unsafe=0

    while IFS= read -r -d '' size && IFS= read -r -d '' rel; do
        [ -z "$rel" ] && continue
        n=$((n+1))

        if ! safe_rel "$rel"; then
            log "[SEC] 跳过不安全路径: $rel"
            skip_unsafe=$((skip_unsafe+1))
            continue
        fi

        local dst="$DST_ROOT/$rel"
        if [ -f "$dst" ]; then
            local dst_size
            dst_size=$(stat -c%s "$dst" 2>/dev/null || echo 0)
            if [ "$dst_size" -eq "$size" ]; then
                log "[SKIP] 目标已存在且大小一致: $rel"
                skip_exist=$((skip_exist+1))
                continue
            fi
        fi

        if [ "$size" -gt "$WATERMARK_BYTES" ]; then
            log "[INFO] 单文件超水位: $rel ($(numfmt --to=iec-i --suffix=B "$size"))"
            flush_batch
            BATCH_RELS=("$rel")
            BATCH_BYTES=$size
            flush_batch
            total_files=$((total_files+1))
            total_bytes=$((total_bytes+size))
            continue
        fi

        if [ "$BATCH_BYTES" -gt 0 ] && \
           [ $((BATCH_BYTES + size)) -gt "$WATERMARK_BYTES" ]; then
            flush_batch
        fi

        BATCH_RELS+=("$rel")
        BATCH_BYTES=$((BATCH_BYTES + size))
        total_files=$((total_files+1))
        total_bytes=$((total_bytes+size))
    done < <(find "$SRC_ROOT" -type f -printf '%s\0%P\0')

    flush_batch

    log "===== 结束 ====="
    log "  扫描文件总数: $n"
    log "  计划迁移:     $total_files 个, 合计 $(numfmt --to=iec-i --suffix=B "$total_bytes")"
    log "  跳过-目标已存在: $skip_exist"
    log "  跳过-路径不安全: $skip_unsafe"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "  提示: 本次为模拟模式，未写入任何文件。加 -r 才真正执行。"
    fi
}

trap 'log "[INT] 被中断"; exit 1' INT TERM
main
