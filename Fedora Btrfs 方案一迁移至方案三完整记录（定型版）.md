# Fedora Btrfs 方案一迁移至方案三完整记录（定型版）

> 记录一次完整的 Btrfs 子卷迁移过程。
> 环境：Fedora 44 KDE / UEFI / Btrfs 无独立 `/boot` 分区。
> **起点**：方案一（所有文件全堆在 Btrfs 顶层 ID 5）。
> **终点**：方案三（`@` 作根，`boot` 作独立子卷，默认子卷保持顶层 ID 5）。
> **核心原则**：GRUB 挂在顶层，BLS 视角天然对齐，内核写死 `subvol=@`。

---

## 一、系统信息

| 项目 | 值 |
| :--- | :--- |
| 系统 | Fedora Linux 44 (KDE Plasma Desktop Edition) |
| 启动方式 | UEFI |
| Btrfs 分区 | `/dev/nvme0n1p5` |
| Btrfs UUID | `c59584d6-9b5b-44e6-8c8b-e224e7eb52d1` |
| EFI 分区 | `/dev/nvme0n1p1` |
| 迁移前状态 | 全部文件在 Btrfs 顶层（ID 5） |
| 迁移后状态 | `@` 作根，`boot` 作独立子卷，默认子卷 ID 5 |
| 机器 ID | `27190949dd9045b68e69178ad9f17257` |

---

## 二、迁移背景与目标

**为什么从方案一（全堆顶层）迁移？**

- 方案一无法做子卷级快照回滚（顶层不能替换）。
- 方案二（`@` 包含 `/boot`）需要欺骗 EFI 壳和 BLS，依赖默认子卷 `@`，维护成本极高。
- 方案三（`@` + `boot` 独立）完美模拟官方“独立 `/boot`”逻辑，且 GRUB 零修改成本。

**目标**：

- 全量快照 `@`（含 `/etc`、`/home`），但不含 `/boot`。
- 回滚只换 `@` 名字，`/boot` 和 GRUB 完全不受影响。
- 不依赖任何 openSUSE 补丁（如 `btrfs_subvol`），不需要 `btrfs_relative_path`。
- 默认子卷永远保持顶层 ID 5，GRUB 靠原生行为找 `boot` 子卷。

---

## 三、核心设计原则

1. **名字 `@` 是根的唯一锚点。**
   - `fstab` 写 `subvol=/@`
   - 内核写 `rootflags=subvol=@`

2. **`boot` 是独立子卷，不跟 `@` 一起快照。**
   - 回滚 `@` 时 `/boot` 稳如泰山。

3. **默认子卷永远为顶层（ID 5）。**
   - GRUB 挂载默认子卷 = 顶层。
   - 顶层下天然看到 `boot` 子卷。
   - BLS 卡片写 `/boot/vmlinuz-...`，GRUB 从顶层看正好进入 `boot` 子卷，路径完美对齐。
   - 不需要欺骗 GRUB，不需要管默认子卷是谁。

4. **元数据自包含。**
   - 每个快照根目录放 `info.txt`，不依赖外部数据库。

---

## 四、迁移全过程（Live CD 环境）

### 步骤 1：挂载 Btrfs 顶层与 ESP

```bash
sudo mkdir -p /mnt
sudo mount -t btrfs -o subvolid=5 /dev/nvme0n1p5 /mnt
sudo mount /dev/nvme0n1p1 /mnt/boot/efi
```

此时 `/mnt` 下就是你的整个系统（`bin`、`etc`、`home`、`boot` 等全在顶层，没有子卷）。

### 步骤 2：创建子卷 `@` 和 `boot`

```bash
cd /mnt
sudo btrfs subvolume create @
sudo btrfs subvolume create boot
```

### 步骤 3：迁移系统文件（核心魔法）

```bash
# 1. 把顶层所有的系统目录（除了新创建的 @ 和 boot）全部移进 @
sudo mv bin dev etc home lib lib64 media mnt opt proc root run sbin srv sys tmp usr var @/

# 2. 把顶层的 boot 目录里的内容，全部移进新创建的 boot 子卷
sudo mv boot/* boot/.[!.]* boot/ 2>/dev/null

# 3. 删掉顶层那个已经空了的 boot 目录
sudo rmdir boot

# 4. 验证顶层状态
ls -la /mnt
# 应该只能看到 @ 和 boot 两个目录（子卷）
```

### 步骤 4：修改 `/etc/fstab`

```bash
sudo nano /mnt/etc/fstab
```

修改为：

```text
UUID=c59584d6-9b5b-44e6-8c8b-e224e7eb52d1 /      btrfs subvol=/@,defaults,ssd,discard=async 0 0
UUID=c59584d6-9b5b-44e6-8c8b-e224e7eb52d1 /boot  btrfs subvol=/boot,defaults,ssd,discard=async 0 0
UUID=c59584d6-9b5b-44e6-8c8b-e224e7eb52d1 /btrfs btrfs subvolid=5,nosuid,nodev,ssd,discard=async 0 0
UUID=F0D8-75CA                            /boot/efi vfat umask=0077,shortname=winnt 0 2
```

### 步骤 5：确认内核参数

```bash
cat /mnt/etc/kernel/cmdline
```

确保里面有 `rootflags=subvol=@`。若没有，手动加上：

```text
root=UUID=c59584d6-... ro rhgb quiet panic=5 rootflags=subvol=@
```

### 步骤 6：重建 initramfs 与 GRUB

```bash
for dir in /dev /dev/pts /proc /sys /run; do
    sudo mount --bind $dir /mnt$dir
done

sudo chroot /mnt
grub2-mkconfig -o /boot/grub2/grub.cfg
dracut -f --regenerate-all
exit
```

### 步骤 7：设置默认子卷为顶层（确认）

```bash
sudo mkdir -p /mnt2
sudo mount -t btrfs -o subvolid=5 /dev/nvme0n1p5 /mnt2
sudo btrfs subvolume get-default /mnt2
# 期望：ID 5 (FS_TREE)
```

如果不是 ID 5，设回来：

```bash
sudo btrfs subvolume set-default 5 /mnt2
```

### 步骤 8：卸载并重启

```bash
sudo umount -R /mnt
sudo umount /mnt2
sudo reboot
```

---

## 五、最终架构布局

```text
ID 5（默认子卷，顶层）
├── @                 ← 根系统，跟快照走
│   ├── etc/
│   ├── home/
│   ├── usr/
│   └── ...
├── boot              ← /boot，独立，不跟快照
│   ├── vmlinuz-...
│   ├── initramfs-...
│   ├── grub2/
│   └── loader/
├── @snap-...
└── @broken-...
```

---

## 六、启动流程（方案三）

```text
UEFI 固件
  ↓
shimx64.efi
  ↓
grubx64.efi（GRUB 程序本体）
  ↓
EFI 壳 /boot/efi/EFI/fedora/grub.cfg
  ↓ insmod btrfs
  ↓ search --fs-uuid --set=dev
  ↓ set prefix=($dev)/boot/grub2
GRUB 挂载默认子卷 = ID 5（顶层）
  ↓ 顶层下有 boot 子卷
  ↓ 从 /boot/grub2/grub.cfg 读菜单
BLS 卡片 /boot/loader/entries/*.conf
  ↓ linux /boot/vmlinuz-...（从顶层看，进入 boot 子卷）
GRUB 加载内核 + initrd
  ↓ 传 options: rootflags=subvol=@
内核挂载 @ 子卷为 /
  ↓
systemd 按 fstab 挂 boot 子卷到 /boot
  ↓
进入系统
```

---

## 七、方案一 vs 方案三 对比

| 维度 | 方案一（全堆顶层） | 方案三（当前） |
| :--- | :--- | :--- |
| 根 | 顶层（ID 5） | `@` |
| `/boot` | 顶层里的 `boot` 目录 | 独立子卷 `boot` |
| 默认子卷 | 顶层（ID 5） | 顶层（ID 5） |
| 能快照回滚吗 | ❌ 不能 | ✅ 能 |
| 快照含 `/boot` 吗 | — | ❌ 不含 |
| 依赖 `btrfs_relative_path` | 否 | 否 |
| 依赖默认子卷具体是谁 | 顶层 | 顶层 |
| GRUB 行为原生度 | 最高 | 高 |
| `/boot` 独立维护 | 否 | 是 |

---

## 八、日常操作（定型）

### 1. 创建快照

```bash
cd /btrfs
sudo btrfs subvolume snapshot @ @-$(date +%Y%m%d)
echo "这是主系统快照 $(date +%F)" | sudo tee /btrfs/@-$(date +%Y%m%d)/info.txt
```

### 2. 回滚（替换式）

```bash
cd /btrfs
sudo mv @ @broken-$(date +%Y%m%d-%H%M%S)
sudo mv @-20260923 @
# 默认子卷已是 ID 5，不用动
sudo reboot
```

**关键**：`set-default` 不用做，因为默认是顶层，本来就对。

### 3. 删除旧子卷

```bash
sudo btrfs subvolume delete /btrfs/@broken-20260923
```

不要用 `rm -rf`。

### 4. 验证状态

```bash
findmnt -no SOURCE,FSTYPE,OPTIONS /
findmnt -no SOURCE,FSTYPE,OPTIONS /boot
findmnt -no SOURCE,FSTYPE,OPTIONS /boot/efi
sudo btrfs subvolume get-default /btrfs
sudo btrfs subvolume list /btrfs
cat /proc/cmdline
```

---

## 九、使用纪律（不可破）

> **默认子卷必须永远保持 ID 5（顶层）。**

- 不要将默认子卷设成 `@` 或任何快照。
- EFI 壳里不能出现 `@`，路径要从顶层看（`($dev)/boot/grub2`）。
- `/etc/fstab` 里根写 `subvol=/@`，`/boot` 写 `subvol=/boot`。
- 回滚只换 `@` 名字，不碰 `boot` 子卷，不碰默认子卷，不碰 EFI 壳。

---

## 十、故障排查

| 现象 | 原因 | 修复 |
|---|---|---|
| 直接进 `grub>` | 默认子卷不是 ID 5 | Live CD：`btrfs subvolume set-default 5 /mnt` |
| 进 `grub>` 且 `ls ($dev)/boot` 报错 | EFI 壳里路径写死 `@` | 去掉 `@`，从顶层看 |
| 菜单可见但报 `file not found` | 默认子卷不是顶层 | 同上 |
| 进 emergency mode | fstab 里 `/boot` 写错 | 改 `subvol=/boot` |
| 卡在 initramfs | initramfs 找不到根 | `dracut -f --regenerate-all` |

### Live USB 修复

```bash
sudo mount -t btrfs -o subvol=@ /dev/nvme0n1p5 /mnt
sudo mount -t btrfs -o subvol=/boot /dev/nvme0n1p5 /mnt/boot
sudo mount /dev/nvme0n1p1 /mnt/boot/efi
sudo mkdir -p /mnt2
sudo mount -t btrfs -o subvolid=5 /dev/nvme0n1p5 /mnt2

# 检查并设默认子卷
sudo btrfs subvolume get-default /mnt2
sudo btrfs subvolume set-default 5 /mnt2

# 改配置、重建 grub、dracut
```

---

## 十一、核心原理一句话

**GRUB 挂在顶层，天然看到 `boot` 子卷，BLS 路径完美对齐；内核写死 `subvol=@`，自己挂载根。两者互不干涉，GRUB 完全不知道 `/` 变了。日常回滚只 `mv` `@` + `reboot`，配置文件全不动，默认子卷永远是顶层。**

---

**定型日期**：2026-09-23  
**系统**：Fedora 44 KDE / UEFI / Btrfs  
**架构**：`@` 跟快照走，`boot` 独立，默认子卷 = 顶层 ID 5  
**状态**：已完成迁移，稳定运行
