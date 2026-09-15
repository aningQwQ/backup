---

# Btrfs 全量快照系统方案（定型版）

## 一、方案定位

一句话：

> **GRUB 跟默认子卷走，内核写死 `subvol=@`，回滚只换子卷名和默认子卷，配置文件全不动。**

目标：

- 全量系统快照，包含 `/etc`、`/home`、`/boot`，无独立 `/boot` 分区
- 回滚时零配置改动
- 不依赖 Snapper、不依赖数据库、不依赖守护进程
- Live USB 可救援
- 防炸、防误触

---

## 二、核心设计原则

1. **名字 `@` 是唯一锚点。**
   - `fstab` 写 `subvol=/@`
   - 内核写 `rootflags=subvol=@`
   - 所有快照最终都通过改名成 `@` 来成为当前系统

2. **GRUB 动态，内核静态。**
   - GRUB：`btrfs_relative_path="yes"`，跟默认子卷解析路径
   - 内核：`rootflags=subvol=@`，写死
   - 两者通过名字 `@` 对齐

3. **回滚只动 Btrfs 层。**
   - `mv` 改子卷名
   - `set-default` 改默认子卷
   - `fstab`、GRUB、BLS 卡片、EFI 壳全不动

4. **元数据自包含。**
   - 每个子卷根目录放 `info.txt`
   - 不依赖任何外部数据库
   - Live USB 挂载后 `cat` 即知

5. **全量快照。**
   - 快照包含整个根子卷
   - 回滚后直接可启动，不用补配置

---

## 三、启动链条

```text
UEFI 固件
  ↓
shimx64.efi
  ↓
grubx64.efi
  ↓
EFI 壳 /boot/efi/EFI/fedora/grub.cfg
  ↓ search --fs-uuid --set=dev
  ↓ set btrfs_relative_path="yes"
  ↓ set prefix=($dev)/boot/grub2
系统盘 /boot/grub2/grub.cfg
  ↓ btrfs_relative_path="yes"
  ↓ insmod blscfg / blscfg
BLS 卡片 /boot/loader/entries/*.conf
  ↓ linux /boot/vmlinuz-...（GRUB 从默认子卷解析）
GRUB 加载内核 + initrd
  ↓ 传 options: rootflags=subvol=@
内核挂载 @ 子卷为 /
  ↓
进入系统
```

---

## 四、关键文件（定型）

### 1. EFI 壳

`/boot/efi/EFI/fedora/grub.cfg`

```text
search --no-floppy --root-dev-only --fs-uuid --set=dev c59584d6-9b5b-44e6-8c8b-e224e7eb52d1
set btrfs_relative_path="yes"
set prefix=($dev)/boot/grub2
export $prefix
configfile $prefix/grub.cfg
```

### 2. GRUB 永久开关

`/etc/grub.d/01_btrfs_relative`

```sh
#!/bin/sh
exec tail -n +3 $0
set btrfs_relative_path="yes"
```

```bash
sudo chmod +x /etc/grub.d/01_btrfs_relative
```

### 3. 内核参数

`/etc/kernel/cmdline`

```text
root=UUID=c59584d6-9b5b-44e6-8c8b-e224e7eb52d1 ro rhgb quiet panic=5 rootflags=subvol=@
```

### 4. fstab

`/etc/fstab`

```text
UUID=c59584d6-... /      btrfs subvol=/@,defaults,ssd,discard=async 0 0
UUID=c59584d6-... /btrfs btrfs subvolid=5,nosuid,nodev,ssd,discard=async 0 0
```

### 5. 子卷标记

每个子卷根目录放 `info.txt`：

```text
这是主系统
2026-09-15
snap123123
```

---

## 五、日常操作（定型）

### 1. 查看

```bash
lsblk -f
findmnt -t btrfs -o TARGET,SOURCE,FSTYPE,OPTIONS
sudo btrfs subvolume list /btrfs
sudo btrfs subvolume get-default /btrfs
cat /proc/cmdline
cat /etc/fstab
cat /info.txt
```

### 2. 创建快照

```bash
sudo btrfs subvolume snapshot / /btrfs/@snap-$(date +%Y%m%d)
echo "这是主系统更新前的备份 $(date +%F)" | sudo tee /btrfs/@snap-$(date +%Y%m%d)/info.txt
```

### 3. 回滚（替换式）

```bash
sudo btrfs subvolume list /btrfs
sudo mv /btrfs/@ /btrfs/@broken-$(date +%Y%m%d-%H%M%S)
sudo mv /btrfs/@snap-20260915 /btrfs/@
sudo btrfs subvolume set-default <新@的ID> /btrfs
sudo reboot
```

### 4. 回滚（保留备份式）

```bash
sudo btrfs subvolume list /btrfs
sudo mv /btrfs/@ /btrfs/@broken-$(date +%Y%m%d-%H%M%S)
sudo btrfs subvolume snapshot /btrfs/@snap-20260915 /btrfs/@
sudo btrfs subvolume list /btrfs
sudo btrfs subvolume set-default <新@的ID> /btrfs
sudo reboot
```

区别：

- 替换式：备份变成当前系统，备份名字消失
- 保留式：备份原样保留，另生成一个新 `@`

### 5. 删除旧子卷

```bash
sudo btrfs subvolume delete /btrfs/@broken-20260915
```

**不要用 `rm`，不要用 `rm -rf`。**

### 6. 重启后验证

```bash
findmnt -no SOURCE,FSTYPE,OPTIONS /
sudo btrfs subvolume get-default /btrfs
sudo btrfs subvolume list /btrfs
cat /proc/cmdline
cat /info.txt
```

---

## 六、使用纪律（不可破）

> **`mv` 和 `set-default` 必须同时做，且指向同一个子卷。**

- 只 `mv` 不 `set-default`：GRUB 加载旧内核，内核挂新根，启动错乱。
- 只 `set-default` 不 `mv`：GRUB 从新系统找内核，内核挂旧根，同样错乱。
- 两者都做：GRUB 和内核指向同一个子卷，无缝启动。

补充：

- `fstab` 根挂载必须写 `subvol=/@`，不能写 `subvolid=256`。
- 内核参数必须写 `rootflags=subvol=@`，不能写 `subvolid=256`。
- EFI 壳里不能写死 `/@/boot/grub2`，要用 `($dev)/boot/grub2`。

---

## 七、故障排查

| 现象 | 原因 | 修复 |
|---|---|---|
| 直接进 `grub>` | EFI 壳找不到主配置 | 修 EFI 壳，去掉写死的 `@` |
| 菜单可见但报 `file not found` | 缺 `btrfs_relative_path` | 确认 `01_btrfs_relative` 存在且生效 |
| 进 emergency mode | `subvol=` 写错 | 改 `/etc/kernel/cmdline` 和 BLS 卡片 |
| 卡在 initramfs | initramfs 找不到根 | `sudo dracut -f` 重建 |

### `grub>` 下手动引导

```text
insmod btrfs
set root=(hd1,gpt5)
set prefix=($root)/@/boot/grub2
configfile $prefix/grub.cfg
```

### Live USB 修复

```bash
sudo mount -t btrfs -o subvol=@ /dev/nvme0n1p5 /mnt
sudo mount /dev/nvme0n1p1 /mnt/boot/efi

for dir in /dev /dev/pts /proc /sys /run; do
    sudo mount --bind $dir /mnt$dir
done

sudo chroot /mnt
# 修配置
grub2-mkconfig -o /boot/grub2/grub.cfg
exit
sudo umount -R /mnt
sudo reboot
```

---

## 八、工具分工

| 工具 | 改什么 | 什么时候用 |
|---|---|---|
| `btrfs` | 子卷、快照、默认子卷 | 迁移、快照、回滚 |
| `grub2-mkconfig` | `/boot/grub2/grub.cfg` | 改 `/etc/grub.d/` 或 `/etc/default/grub` 后 |
| `grubby` | BLS 卡片 + `/etc/default/grub` | 改现有内核参数 |
| `kernel-install` | BLS 卡片 + initramfs | 装/删内核时自动 |
| `dracut` | initramfs | 改 fstab 或根分区后 |
| `efibootmgr` | UEFI 启动项 | 管理启动顺序 |

核心原则：

- 改现有内核参数 → `grubby`
- 改未来新内核参数 → `/etc/kernel/cmdline`
- 改菜单结构 → `/etc/grub.d/` + `grub2-mkconfig`

---

## 九、文件职责

| 文件 | 谁生成 | 职责 | 换子卷名时 |
|---|---|---|---|
| `/boot/efi/EFI/fedora/grub.cfg` | 安装器 / 手动 | 指路 | 不动 |
| `/boot/grub2/grub.cfg` | `grub2-mkconfig` | 菜单 + 开关 | 不动 |
| `/etc/grub.d/01_btrfs_relative` | 手动 | 永久开关 | 不动 |
| `/etc/default/grub` | 手动 | 菜单设置 | 不动 |
| `/etc/kernel/cmdline` | 手动 | 新内核参数模板 | 不动 |
| `/boot/loader/entries/*.conf` | `kernel-install` | 具体启动项 | 不动 |
| `/etc/fstab` | 手动 | 系统挂载配置 | 不动 |

关键洞察：

> **日常快照回滚不需要改任何配置文件，因为 `@` 这个名字始终不变。**

---

## 十、能力边界

### 能防

- 系统更新炸了 → 回滚快照
- 配置改坏了 → 回滚快照
- 误删系统文件 → 回滚快照
- 误改默认子卷 → 内核仍找 `@`
- Snapper 数据库损坏 → 无数据库，无影响
- EFI 分区损坏 → 只要有 GRUB 能跑，就能从 Btrfs 加载 `@`

### 不能防

- Btrfs 分区本身损坏 → 快照和系统同分区，一起坏
- 磁盘物理故障 → 快照同盘，盘挂全没
- 快照被误删 → `btrfs subvolume delete` 删了就没了
- EFI 分区物理损坏 → 快照不包含它，要重建

### 要防硬件炸

把快照 `btrfs send` 到另一块盘或远程：

```bash
sudo btrfs send /btrfs/@snap-20260915 | sudo btrfs receive /mnt/backup/
```

---

## 十一、和其他方案对比

| 维度 | openSUSE Snapper | 本方案 |
|---|---|---|
| 元数据 | 集中式数据库 `/var/lib/snapper` | 分布式 `info.txt` 跟子卷走 |
| 回滚 | `snapper rollback` | `mv` + `set-default` |
| 依赖 | `snapperd`、数据库、GRUB 补丁 | 纯 Btrfs 原生命令 |
| 数据库损坏 | 恢复要先研究数据库 | 无数据库，无影响 |
| 手动 `mv` 子卷 | Snapper 会懵 | 正常，名字就是设计的一部分 |
| Live USB 恢复 | 要解析数据库 | `cat info.txt` 即可 |
| 自动化 | `zypper` 集成，自动快照 | 手动，可脚本化 |
| 防误触 | 依赖工具纪律 | 原生命令，透明可控 |

---

## 十二、方案定型总结

**架构：**

> GRUB 跟默认子卷，内核写死 `@`，回滚只动 Btrfs 层。

**操作：**

> `mv` + `set-default` + `reboot`，配置文件全不动。

**元数据：**

> `info.txt` 跟子卷走，不依赖数据库。

**防炸：**

> 全量快照，包含 `/etc`、`/home`、`/boot`，回滚后直接启动。

**防误触：**

> 原生命令，透明可控，不依赖 Snapper 数据库和 openSUSE 补丁。

**边界：**

> 防系统层面的炸和误操作，不防硬件层面的炸。要防硬件炸，`btrfs send` 到外部。

**一句话：**

> 部署难，使用简单，堪称优雅。GRUB 不用管，内核锁定 `@`，日常回滚只换子卷名和默认子卷，配置文件一个字不动。

---

**定型日期：2026-09-15**  
**系统：Fedora 44 KDE / UEFI / Btrfs 无独立 `/boot` 分区**  
**状态：已定型，稳定运行**
