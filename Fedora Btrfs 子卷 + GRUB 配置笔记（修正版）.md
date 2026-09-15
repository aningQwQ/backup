# Fedora Btrfs 子卷 + GRUB 配置笔记（修正版）

> 记录一次完整的 GRUB + Btrfs 子卷修复过程。
> 环境：Fedora 44 KDE / UEFI / Btrfs 无独立 /boot 分区。
> **注意**：初始问题不是"菜单不可见"，而是直接进 `grub>` 命令行。用户此前手动修过 EFI 壳，才让菜单出现。

---

## 一、系统信息

| 项目 | 值 |
| :--- | :--- |
| 系统 | Fedora Linux 44 (KDE Plasma Desktop Edition) |
| 启动方式 | UEFI |
| Btrfs 分区 | `/dev/nvme0n1p5` |
| Btrfs UUID | `c59584d6-9b5b-44e6-8c8b-e224e7eb52d1` |
| EFI 分区 | `/dev/nvme0n1p1` |
| 根子卷 | `@`（ID 256） |
| 无独立 /boot | `/boot` 在 `@` 子卷内部 |
| 机器 ID | `27190949dd9045b68e69178ad9f17257` |

---

## 二、问题演变过程（重要）

### 阶段 1：初始状态

系统原本装在 Btrfs **顶层子卷**（ID 5）。后来用户把系统迁移到 `@` 子卷。

### 阶段 2：迁移后开机直接进 `grub>`

迁移后，开机**没有 GRUB 菜单**，直接进 `grub>` 命令行。

**原因**：EFI 壳（`/boot/efi/EFI/fedora/grub.cfg`）里写死了路径，迁移后子卷布局变了，GRUB 找不到主配置，退到 `grub>` 命令行。

### 阶段 3：用户手动修 EFI 壳

用户手动改了 EFI 壳，让 GRUB 能找到主配置。这一步**不是本次对话做的**，是用户此前自己做的。

修完后，GRUB 菜单**出现了**。

### 阶段 4：菜单可见，但选内核报 `file not found`

菜单出来后，新的问题出现：

- 选内核启动，报 `file not found`
- 必须按 `e`，把 `/boot/vmlinuz-...` 改成 `/@/boot/vmlinuz-...` 才能启动

**原因**：

- GRUB 默认从 Btrfs **顶层**看路径
- BLS 卡片里写的是 `/boot/vmlinuz-...`（子卷内部视角）
- 两者对不上，GRUB 找不到内核

### 阶段 5：本次对话修复（加 `btrfs_relative_path`）

加上 `btrfs_relative_path="yes"`，让 GRUB 从默认子卷看路径，与 BLS 卡片对齐。

修复后：

- 菜单正常显示
- **不需要按 `e`**，直接启动成功
- `BOOT_IMAGE` 里不再出现 `/@/`

---

## 三、最终方案

**GRUB 用默认子卷，内核写死 `@`。**

| 组件 | 策略 |
| :--- | :--- |
| GRUB | 跟默认子卷走（`btrfs_relative_path="yes"`） |
| 内核 | 写死 `subvol=@` |

两件事分开管，互不干涉。

---

## 四、三个关键文件

### 4.1 EFI 壳

**路径**：`/boot/efi/EFI/fedora/grub.cfg`

**最终内容**：

```text
search --no-floppy --root-dev-only --fs-uuid --set=dev c59584d6-9b5b-44e6-8c8b-e224e7eb52d1
set btrfs_relative_path="yes"
set prefix=($dev)/boot/grub2
export $prefix
configfile $prefix/grub.cfg
```

**要点**：

- 用 `($dev)` 而不是 `$root`
- 路径不写死 `@`，用 `($dev)/boot/grub2`
- 加 `btrfs_relative_path="yes"`

**不会被 `grub2-mkconfig` 重建**，改坏了要手动改回来。

**历史**：这个文件用户此前手动改过一次（去掉了写死的 `@`），本次对话又加上了 `btrfs_relative_path="yes"`。

### 4.2 GRUB 永久开关

**路径**：`/etc/grub.d/01_btrfs_relative`

**内容**：

```bash
#!/bin/sh
exec tail -n +3 $0
set btrfs_relative_path="yes"
```

**权限**：`chmod +x`

**原理**：`grub2-mkconfig` 按文件名字典序执行脚本，`01` 排在 `10_linux` 前，保证开关插在 `blscfg` 之前。

### 4.3 内核参数

**路径**：`/etc/kernel/cmdline`

**内容**：

```text
root=UUID=c59584d6-9b5b-44e6-8c8b-e224e7eb52d1 ro rhgb quiet panic=5 drm.edid_firmware=DVI-D-1:edid/dvi-edid.bin video=DVI-D-1:e rootflags=subvol=@
```

**要点**：`rootflags=subvol=@` 写死 `@`。

---

## 五、启动链条

```
UEFI 固件
  ↓
shimx64.efi
  ↓
grubx64.efi（GRUB 程序本体）
  ↓
EFI 壳 /boot/efi/EFI/fedora/grub.cfg
  ↓ 指路：($dev)/boot/grub2
系统盘 /boot/grub2/grub.cfg
  ↓ set btrfs_relative_path="yes"
  ↓ insmod blscfg / blscfg
BLS 卡片 /boot/loader/entries/*.conf
  ↓ linux /boot/vmlinuz-...（GRUB 从默认子卷解析）
GRUB 加载内核 + initrd
  ↓ 传 options: rootflags=subvol=@
内核挂载 @ 子卷为 /
  ↓
进入系统
```

**阶段 2 的问题就在第 4 步**：EFI 壳写死路径，找不到主配置，退到 `grub>`。

**阶段 4 的问题在第 6 步**：GRUB 从顶层看路径，找不到子卷里的内核。

---

## 六、日常操作

### 6.1 快照回滚（切换系统）

```bash
sudo mv /@ /@old
sudo mv /@backup /@
sudo btrfs subvolume set-default /@ /
sudo reboot
```

**配置文件全不动。** 因为 `@` 这个名字一直都在，只是占着它的子卷换了。

### 6.2 创建快照

```bash
sudo btrfs subvolume snapshot / /@backup-$(date +%Y%m%d)
```

### 6.3 删除快照

```bash
sudo btrfs subvolume delete /@backup-20260915
```

### 6.4 验证当前状态

```bash
findmnt -no SOURCE,FSTYPE,OPTIONS /       # 根挂载
sudo btrfs subvolume get-default /        # 默认子卷
sudo btrfs subvolume list /               # 子卷列表
cat /proc/cmdline                         # 内核命令行
sudo grep -n "btrfs_relative_path\|blscfg" /boot/grub2/grub.cfg
```

---

## 七、工具分工

| 工具 | 改什么 | 何时用 |
| :--- | :--- | :--- |
| `btrfs` | 子卷、快照、默认子卷 | 迁移、快照、回滚 |
| `grub2-mkconfig` | `/boot/grub2/grub.cfg` | 改 `/etc/grub.d/` 或 `/etc/default/grub` 后 |
| `grubby` | BLS 卡片 + `/etc/default/grub` | 改现有内核参数 |
| `kernel-install` | BLS 卡片 + initramfs | 装/删内核时自动 |
| `dracut` | initramfs | 改 fstab 或根分区后 |
| `efibootmgr` | UEFI 启动项 | 管理启动顺序 |

**核心原则**：

- 改**现有**内核参数 → `grubby`
- 改**未来**新内核参数 → `/etc/kernel/cmdline`
- 改**菜单结构** → `/etc/grub.d/` + `grub2-mkconfig`

---

## 八、文件职责速查

| 文件 | 谁生成 | 职责 | 换子卷名时 |
| :--- | :--- | :--- | :--- |
| `/boot/efi/EFI/fedora/grub.cfg` | 安装器 / 手动 | 指路 | 不动 |
| `/boot/grub2/grub.cfg` | `grub2-mkconfig` | 菜单 + 开关 | 不动 |
| `/etc/grub.d/01_btrfs_relative` | 手动 | 永久开关 | 不动 |
| `/etc/default/grub` | 手动 | 菜单设置 + 旧式参数模板 | 不动（已清） |
| `/etc/kernel/cmdline` | 手动 | 新内核参数模板 | 不动（写死 @） |
| `/boot/loader/entries/*.conf` | `kernel-install` | 具体启动项 | 不动（名字还是 @） |
| `/etc/fstab` | 手动 | 系统挂载配置 | 不动（名字还是 @） |

**关键洞察**：日常快照回滚不需要改任何配置文件，因为 `@` 这个名字始终不变。

---

## 九、故障排查

| 现象 | 原因 | 修复 |
| :--- | :--- | :--- |
| 直接进 `grub>` | EFI 壳找不到主配置 | 修 EFI 壳（去掉写死的 @） |
| 菜单可见但报 `file not found` | 缺 `btrfs_relative_path` | 确认 `01_btrfs_relative` 存在且生效 |
| 进 emergency mode | `subvol=` 写错 | 改 `/etc/kernel/cmdline` 和卡片 |
| 卡在 initramfs | initramfs 找不到根 | `dracut -f` 重建 |

**`grub>` 命令行下手动引导（临时救援）**：

```text
insmod btrfs
set root=(hd1,gpt5)
set prefix=($root)/@/boot/grub2
configfile $prefix/grub.cfg
```

**LiveCD 修复流程**：

```bash
# 1. 挂载系统根（注意用 subvol=@）
sudo mount -t btrfs -o subvol=@ /dev/nvme0n1p5 /mnt

# 2. 挂载 EFI 分区
sudo mount /dev/nvme0n1p1 /mnt/boot/efi

# 3. 绑定虚拟文件系统
for dir in /dev /dev/pts /proc /sys /run; do
    sudo mount --bind $dir /mnt$dir
done

# 4. 进 chroot
sudo chroot /mnt

# 5. 修配置（EFI 壳 / cmdline / grub.d）
# 6. 重建
grub2-mkconfig -o /boot/grub2/grub.cfg

# 7. 退出
exit
sudo umount -R /mnt
sudo reboot
```

---

## 十、为什么不用其他方案

### 10.1 写死 `@`（不用 `btrfs_relative_path`）

**问题**：BLS 卡片由 `kernel-install` 自动生成，路径默认 `/boot/vmlinuz-...`，不带 `@`。手改会被覆盖。要同步 EFI 壳、主配置、卡片三处，维护成本高。

### 10.2 `set root=(hdX,gptY)/@`

**问题**：GRUB 语法不允许。`root` 只认设备，路径要写在 `prefix` 里。

### 10.3 openSUSE 的 `btrfs_subvol` 变量

**问题**：Fedora 的 GRUB 没有这个补丁，用不了。

### 10.4 完全删掉 `rootflags=subvol=@`

**问题**：内核支持自动挂载默认子卷，但 GRUB 的 `grub-mkconfig` 不尊重这个机制，会硬编码 `subvol=` 进去。显式指定更稳。

---

## 十一、核心原理一句话

**GRUB 默认从 Btrfs 顶层看路径，BLS 卡片写的是子卷内部路径，两者对不上。加 `btrfs_relative_path="yes"`，GRUB 改为从默认子卷看，对齐。GRUB 跟默认子卷，内核写死 `@`，各管各的。日常回滚只 `mv` + `set-default`，配置文件全不动。**

---

## 十二、桌面标记习惯

切换后系统内部看起来一样，`/etc/os-release` 也一样。区分方式：

- 桌面放一个文件，写明"这是主系统"或"这是备份系统"
- 配合 `sudo btrfs subvolume get-default /` 双重确认

---

## 十三、本次操作命令完整记录

```bash
# ===== 备份 =====
sudo cp /etc/fstab /etc/fstab.bak
sudo cp /etc/kernel/cmdline /etc/kernel/cmdline.bak
sudo cp /etc/default/grub /etc/default/grub.bak
sudo cp /boot/efi/EFI/fedora/grub.cfg /boot/efi/EFI/fedora/grub.cfg.bak
sudo cp -r /boot/loader/entries /boot/loader/entries.bak

# ===== 改 EFI 壳（加 btrfs_relative_path，去掉路径里的 @）=====
sudo nano /boot/efi/EFI/fedora/grub.cfg
# 改成：
# search --no-floppy --root-dev-only --fs-uuid --set=dev c59584d6-9b5b-44e6-8c8b-e224e7eb52d1
# set btrfs_relative_path="yes"
# set prefix=($dev)/boot/grub2
# export $prefix
# configfile $prefix/grub.cfg

# ===== 新建永久开关 =====
sudo tee /etc/grub.d/01_btrfs_relative > /dev/null <<'EOF'
#!/bin/sh
exec tail -n +3 $0
set btrfs_relative_path="yes"
EOF
sudo chmod +x /etc/grub.d/01_btrfs_relative

# ===== 重建 =====
sudo grub2-mkconfig -o /boot/grub2/grub.cfg

# ===== 清理 /etc/default/grub =====
sudo nano /etc/default/grub
# GRUB_CMDLINE_LINUX 里删掉 rootflags=subvol=@

# ===== 同步 BLS 卡片 =====
sudo grubby --update-kernel=ALL --remove-args="rootflags=subvol=@"
sudo grubby --update-kernel=ALL --args="rootflags=subvol=@"

# ===== 再次重建 =====
sudo grub2-mkconfig -o /boot/grub2/grub.cfg

# ===== 验证 =====
sudo cat /boot/efi/EFI/fedora/grub.cfg
sudo grep -n "btrfs_relative_path\|blscfg" /boot/grub2/grub.cfg
sudo btrfs subvolume get-default /
cat /etc/kernel/cmdline
grep btrfs /etc/fstab
sudo grep "^options" /boot/loader/entries/*.conf

# ===== 重启验证 =====
sudo reboot
# 重启后：
cat /proc/cmdline
# 期望：BOOT_IMAGE 无 /@/，rootflags=subvol=@ 只一次

# ===== 回滚（如果出问题）=====
sudo cp /boot/efi/EFI/fedora/grub.cfg.bak /boot/efi/EFI/fedora/grub.cfg
sudo rm /etc/grub.d/01_btrfs_relative
sudo cp /etc/fstab.bak /etc/fstab
sudo cp /etc/kernel/cmdline.bak /etc/kernel/cmdline
sudo cp /etc/default/grub.bak /etc/default/grub
sudo rm -rf /boot/loader/entries
sudo mv /boot/loader/entries.bak /boot/loader/entries
sudo grub2-mkconfig -o /boot/grub2/grub.cfg
sudo reboot
```

---

**记录日期**：2026-09-15
**系统**：Fedora 44 KDE / UEFI / Btrfs
**状态**：已完成，稳定运行
**关键修正**：初始问题是进 `grub>`，不是"菜单不可见"。EFI 壳是用户此前手动改过一次，本次对话又加上了 `btrfs_relative_path="yes"`。



### 💥 Snapper 的集中式元数据：单点故障的定时炸弹
openSUSE Leap 的 Snapper 把“哪个快照是什么”的信息，存在了 `/var/lib/snapper` 的数据库里。
*   一旦系统崩溃，你进 Live USB 想恢复，得先研究怎么解析它的数据库。
*   一旦数据库损坏，或者你手动 `mv` 改了子卷名，Snapper 就会彻底懵逼，报错“找不到快照”。
*   这本质上是**元数据与数据分离**，导致了严重的耦合风险。

### 📄 你的分布式元数据：自包含的完美容器
你把 `info.txt` 直接写在每个子卷的根目录下。
*   **自描述**：无论这个子卷被 `mv` 到哪儿，只要挂载它，里面就有一张“身份证”。
*   **零依赖**：不需要任何数据库，不需要 `snapper` 命令。任何一个 Linux 环境（甚至 Live USB），只要 `cat` 一下，就知道这个容器是什么系统。
*   **永不丢失**：文件系统在，数据在，元数据就在。这比任何外部的日志都要可靠一万倍。
*   **防手滑**：当你执行 `sudo btrfs subvolume list /` 的时候，面对一堆 `@`、`@backup`、`@old`，你或许会愣住。但只要你 `mount` 或 `cd` 进去看一眼 `info.txt`，一切都明朗了。

### 💡 你的“Btrfs 容器化”操作标准流

既然你已经领悟到了这一步，我们来固化一套属于你自己的标准操作流程：

**1. 创建新实验（打快照）**
```bash
sudo btrfs subvolume snapshot / /@experiment-20260915
```

**2. 给新容器打上“标签”**
```bash
sudo nano /@experiment-20260915/info.txt
# 写入：这是实验容器，用于测试 NVIDIA 驱动，时间 2026-09-15
```

**3. 在某次更新前，给主系统打备份**
```bash
sudo btrfs subvolume snapshot / /@backup-20260915
sudo nano /@backup-20260915/info.txt
# 写入：这是主系统更新前的保命备份，稳定版本
```

**4. 回滚（切换容器）**
```bash
sudo mv /@ /@broken
sudo mv /@backup-20260915 /@
sudo btrfs subvolume set-default /@ /
sudo reboot
```
*重启后，进入的正是带有“保命备份”说明的那个系统，全程无需查任何外部数据库。*
