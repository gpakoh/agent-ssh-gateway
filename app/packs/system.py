from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity
from app.packs import Pack

SYSTEM_DISK_PATTERNS: tuple[DestructivePattern, ...] = (
    # ---- disk: dd ----
    DestructivePattern(
        name="dd-device",
        regex=r"dd\s+.*of=['\"]?/dev/",
        reason="dd to a block device will OVERWRITE all data on that device.",
        severity=Severity.HIGH,
        description="Dangerous! dd to /dev/* block device overwrites data.",
        suggestions=(
            PatternSuggestion(command="lsblk", description="List block devices first"),
            PatternSuggestion(command="dd if={src} of={dst} bs=4M status=progress", description="Use bs=4M and status=progress for safety"),
        ),
    ),
    DestructivePattern(
        name="dd-wipe",
        regex=r"dd\s+.*if=['\"]?/dev/(?:zero|urandom|random).*of=['\"]?/dev/",
        reason="dd from /dev/zero or /dev/urandom to a device will WIPE all data!",
        severity=Severity.HIGH,
        description="dd wipe from /dev/zero|urandom|random to block device.",
        suggestions=(
            PatternSuggestion(command="lsblk -f", description="Check existing filesystems first"),
            PatternSuggestion(command="wipefs -n /dev/{dev}", description="Preview what would be wiped"),
        ),
    ),
    # ---- disk: partition tools ----
    DestructivePattern(
        name="fdisk-edit",
        regex=r"fdisk\s+['\"]?/dev/(?!.*-l)",
        reason="fdisk can modify partition tables and cause data loss.",
        severity=Severity.HIGH,
        description="fdisk edits partition tables on /dev/*.",
        suggestions=(
            PatternSuggestion(command="fdisk -l /dev/{dev}", description="List partition table without editing"),
            PatternSuggestion(command="lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT", description="View partition layout safely"),
        ),
    ),
    DestructivePattern(
        name="parted-modify",
        regex=r"parted\b[^\n;&|]*?['\"]?/dev/\S+['\"]?(?:\s+--)?\s+(?:(?!\s*(?:align-check|help|h|print|p|quit|q|select|unit|u)\b)|[^\n;&|]*\b(?:print|p)\b\s+(?:(?:devices|free|list|all|\d+)\s+\S+|(?!devices\b|free\b|list\b|all\b|\d+\b)\S+)|[^\n;&|]*\b(?:disk_set|disk_toggle|mklabel|mktable|mkpart|name|rescue|resizepart|rm|set|toggle|type)\b)",
        reason="parted can modify partition tables and cause data loss.",
        severity=Severity.HIGH,
        description="parted modifies partition tables.",
        suggestions=(
            PatternSuggestion(command="parted /dev/{dev} print", description="View current partition table"),
            PatternSuggestion(command="parted /dev/{dev} print free", description="Show free space on device"),
        ),
    ),
    # ---- disk: filesystem creation/destruction ----
    DestructivePattern(
        name="mkfs",
        regex=r"mkfs(?:\.[a-z0-9]+)?\s+",
        reason="mkfs formats a partition/device and ERASES all existing data.",
        severity=Severity.HIGH,
        description="mkfs creates a filesystem, erasing existing data.",
        suggestions=(
            PatternSuggestion(command="lsblk -f", description="Check existing filesystem first"),
            PatternSuggestion(command="fsck -N /dev/{dev}", description="Detect existing filesystem type"),
        ),
    ),
    DestructivePattern(
        name="mkswap",
        regex=r"mkswap\s+",
        reason="mkswap formats a partition as swap, ERASING any existing data.",
        severity=Severity.HIGH,
        description="mkswap creates swap area, overwriting existing data.",
        suggestions=(
            PatternSuggestion(command="swapon --show", description="Show current swap devices"),
            PatternSuggestion(command="free -h", description="Check memory and swap usage"),
        ),
    ),
    DestructivePattern(
        name="wipefs",
        regex=r"wipefs\s+",
        reason="wipefs removes filesystem signatures.",
        severity=Severity.HIGH,
        description="wipefs erases filesystem signatures from a device.",
        suggestions=(
            PatternSuggestion(command="wipefs -n /dev/{dev}", description="Preview what signatures would be erased"),
            PatternSuggestion(command="blkid /dev/{dev}", description="Show current filesystem signatures"),
        ),
    ),
    # ---- disk: mount/umount ----
    DestructivePattern(
        name="mount-bind-root",
        regex=r"mount\s+.*--bind\s+.*\s+['\"]?/(?:$|[^a-z])",
        reason="mount --bind to root directory can have system-wide effects.",
        severity=Severity.HIGH,
        description="Mount --bind to / may cause system issues.",
        suggestions=(
            PatternSuggestion(command="mount --bind /source /specific/target", description="Bind mount to a specific directory"),
            PatternSuggestion(command="ln -s /source /target", description="Use symlink as a non-destructive alternative"),
        ),
    ),
    DestructivePattern(
        name="umount-force",
        regex=r"umount\s+.*-[a-z]*f",
        reason="umount -f may cause data loss if device is in use.",
        severity=Severity.HIGH,
        description="Force unmount can cause data loss.",
        suggestions=(
            PatternSuggestion(command="lsof {mnt}", description="Find processes using the mount"),
            PatternSuggestion(command="fuser -v {mnt}", description="Find processes using the mount point"),
        ),
    ),
    DestructivePattern(
        name="losetup-device",
        regex=r"losetup\s+['\"]?/dev/loop",
        reason="losetup modifies loop device associations.",
        severity=Severity.HIGH,
        description="losetup on /dev/loop changes device mapping.",
        suggestions=(
            PatternSuggestion(command="losetup -a", description="List all current loop devices"),
            PatternSuggestion(command="losetup -f --show {file}", description="Let system choose a free loop device"),
        ),
    ),
    # ---- disk: mdadm RAID ----
    DestructivePattern(
        name="mdadm-stop",
        regex=r"mdadm\s+(?:.*\s+)?(?:--stop|-S)\b",
        reason="mdadm --stop shuts down a RAID array.",
        severity=Severity.HIGH,
        description="Stops a RAID array. Data may become inaccessible.",
        suggestions=(
            PatternSuggestion(command="mdadm --detail /dev/md{0}", description="Check array status before stopping"),
            PatternSuggestion(command="cat /proc/mdstat", description="Check RAID status"),
        ),
    ),
    DestructivePattern(
        name="mdadm-remove",
        regex=r"mdadm\s+(?:.*\s+)?--remove\b",
        reason="mdadm --remove removes a drive from a RAID array.",
        severity=Severity.HIGH,
        description="Removes device from RAID. Data loss if no redundancy.",
        suggestions=(
            PatternSuggestion(command="mdadm --detail /dev/md{0}", description="Check array health first"),
            PatternSuggestion(command="smartctl -a /dev/{sdc}", description="Check device SMART status before removal"),
        ),
    ),
    DestructivePattern(
        name="mdadm-fail",
        regex=r"mdadm\s+(?:.*\s+)?(?:--fail|-f)\b",
        reason="mdadm --fail marks a device as failed.",
        severity=Severity.HIGH,
        description="Marks RAID device as failed.",
        suggestions=(
            PatternSuggestion(command="mdadm --detail /dev/md{0}", description="Check array health first"),
            PatternSuggestion(command="smartctl -a /dev/{sdc}", description="Check device SMART status first"),
        ),
    ),
    DestructivePattern(
        name="mdadm-zero-superblock",
        regex=r"mdadm\s+(?:.*\s+)?--zero-superblock\b",
        reason="mdadm --zero-superblock erases RAID metadata.",
        severity=Severity.HIGH,
        description="Erases RAID superblock. Array cannot be reassembled.",
        suggestions=(
            PatternSuggestion(command="mdadm --examine /dev/{sdc}", description="Inspect superblock before erasing"),
            PatternSuggestion(command="mdadm --examine --brief /dev/{sdc}", description="Dump superblock metadata"),
        ),
    ),
    DestructivePattern(
        name="mdadm-create",
        regex=r"mdadm\s+(?:.*\s+)?(?:--create|-C)\b",
        reason="mdadm --create creates a RAID array, erasing data on member devices.",
        severity=Severity.HIGH,
        description="Creates new RAID, erasing existing data on members.",
        suggestions=(
            PatternSuggestion(command="mdadm --detail --scan", description="Save existing array config first"),
            PatternSuggestion(command="wipefs -n /dev/{sdc}", description="Check if disks have existing filesystems"),
        ),
    ),
    DestructivePattern(
        name="mdadm-grow",
        regex=r"mdadm\s+(?:.*\s+)?--grow\b",
        reason="mdadm --grow reshapes a RAID array. Interruption causes data loss.",
        severity=Severity.HIGH,
        description="Grows/reshapes RAID. Backup first.",
        suggestions=(
            PatternSuggestion(command="mdadm --detail /dev/md{0}", description="Check current array layout first"),
            PatternSuggestion(command='echo check > /sys/block/md{0}/md/sync_action', description="Verify array health before growing"),
        ),
    ),
    # ---- disk: btrfs ----
    DestructivePattern(
        name="btrfs-subvolume-delete",
        regex=r"btrfs\b.*?\s+subvolume\s+delete\b",
        reason="btrfs subvolume delete REMOVES a subvolume and its data.",
        severity=Severity.HIGH,
        description="Deletes a btrfs subvolume permanently.",
        suggestions=(
            PatternSuggestion(command="btrfs subvolume list {path}", description="List subvolumes first"),
            PatternSuggestion(command="btrfs subvolume show {path}", description="Show subvolume details"),
        ),
    ),
    DestructivePattern(
        name="btrfs-device-remove",
        regex=r"btrfs\b.*?\s+device\s+(?:remove|delete)\b",
        reason="btrfs device remove redistributes data off a device.",
        severity=Severity.HIGH,
        description="Removes device from btrfs filesystem.",
        suggestions=(
            PatternSuggestion(command="btrfs device usage {mnt}", description="Check device allocation first"),
            PatternSuggestion(command="btrfs filesystem usage {mnt}", description="Check space usage before removal"),
        ),
    ),
    DestructivePattern(
        name="btrfs-device-add",
        regex=r"btrfs\b.*?\s+device\s+add\b",
        reason="btrfs device add incorporates a device. Verify correctness.",
        severity=Severity.HIGH,
        description="Adds device to btrfs filesystem. Verify target.",
        suggestions=(
            PatternSuggestion(command="btrfs filesystem show {mnt}", description="Check current device configuration"),
            PatternSuggestion(command="lsblk", description="Verify new device is correct"),
        ),
    ),
    DestructivePattern(
        name="btrfs-balance",
        regex=r"btrfs\b.*?\s+balance\s+start\b",
        reason="btrfs balance redistributes data. Can be slow and disruptive.",
        severity=Severity.HIGH,
        description="Starts btrfs balance operation.",
        suggestions=(
            PatternSuggestion(command="btrfs balance status {mnt}", description="Check if a balance is already running"),
            PatternSuggestion(command="btrfs balance start -dusage=5 {mnt}", description="Start with data usage filter for safety"),
        ),
    ),
    DestructivePattern(
        name="btrfs-check-repair",
        regex=r"btrfs\b.*?\s+check\s+(?:.*\s+)?--repair\b",
        reason="btrfs check --repair is DANGEROUS. Can cause data loss.",
        severity=Severity.HIGH,
        description="btrfs check --repair modifies filesystem. Backup first!",
        suggestions=(
            PatternSuggestion(command="btrfs check --readonly /dev/{dev}", description="Run read-only check first"),
            PatternSuggestion(command="btrfs device stats {mnt}", description="Check device errors first"),
        ),
    ),
    DestructivePattern(
        name="btrfs-rescue",
        regex=r"btrfs\b.*?\s+rescue\b",
        reason="btrfs rescue modifies filesystem metadata. Last resort only.",
        severity=Severity.HIGH,
        description="btrfs rescue operations modify metadata.",
        suggestions=(
            PatternSuggestion(command="btrfs rescue super-recover -y /dev/{dev}", description="Attempt superblock recovery only"),
            PatternSuggestion(command="btrfs restore -l /dev/{dev}", description="List files without attempting recovery"),
        ),
    ),
    DestructivePattern(
        name="btrfs-filesystem-resize",
        regex=r"btrfs\b.*?\s+filesystem\s+resize\b",
        reason="btrfs filesystem resize can shrink FS. Data loss if too small.",
        severity=Severity.HIGH,
        description="Resizes btrfs filesystem. Can cause data loss if shrinking.",
        suggestions=(
            PatternSuggestion(command="btrfs filesystem usage {mnt}", description="Check space usage before resizing"),
            PatternSuggestion(command="btrfs filesystem show {mnt}", description="Check device sizes before resizing"),
        ),
    ),
    # ---- disk: dmsetup ----
    DestructivePattern(
        name="dmsetup-remove",
        regex=r"dmsetup\b.*?\s+remove\b",
        reason="dmsetup remove detaches a device-mapper device.",
        severity=Severity.HIGH,
        description="Removes a device-mapper device.",
        suggestions=(
            PatternSuggestion(command="dmsetup info {dev}", description="Check device info first"),
            PatternSuggestion(command="dmsetup table {dev}", description="Show current mapping table"),
        ),
    ),
    DestructivePattern(
        name="dmsetup-remove-all",
        regex=r"dmsetup\b.*?\s+remove_all\b",
        reason="dmsetup remove_all REMOVES ALL device-mapper devices.",
        severity=Severity.HIGH,
        description="Removes ALL device-mapper devices. Extremely dangerous!",
        suggestions=(
            PatternSuggestion(command="dmsetup info", description="List ALL device-mapper devices first"),
            PatternSuggestion(command="dmsetup table", description="Show all mappings before bulk removal"),
        ),
    ),
    DestructivePattern(
        name="dmsetup-wipe-table",
        regex=r"dmsetup\b.*?\s+wipe_table\b",
        reason="dmsetup wipe_table replaces table with error target.",
        severity=Severity.HIGH,
        description="Wipes device-mapper table. All I/O will fail.",
        suggestions=(
            PatternSuggestion(command="dmsetup table {dev}", description="Save current table first"),
            PatternSuggestion(command="dmsetup info {dev}", description="Check device status before wiping"),
        ),
    ),
    DestructivePattern(
        name="dmsetup-clear",
        regex=r"dmsetup\b.*?\s+clear\b",
        reason="dmsetup clear removes the mapping table from a device.",
        severity=Severity.HIGH,
        description="Clears device-mapper mapping table.",
        suggestions=(
            PatternSuggestion(command="dmsetup table {dev}", description="Backup current mapping first"),
            PatternSuggestion(command="dmsetup info {dev}", description="Check device status before clearing"),
        ),
    ),
    DestructivePattern(
        name="dmsetup-load",
        regex=r"dmsetup\b.*?\s+load\b",
        reason="dmsetup load changes device mapping.",
        severity=Severity.HIGH,
        description="Loads new device-mapper table. Verify correctness.",
        suggestions=(
            PatternSuggestion(command="dmsetup table {dev}", description="Backup current table first"),
            PatternSuggestion(command="dmsetup info {dev}", description="Check device info before changing"),
        ),
    ),
    DestructivePattern(
        name="dmsetup-create",
        regex=r"dmsetup\b.*?\s+create\b",
        reason="dmsetup create sets up a new device-mapper device.",
        severity=Severity.HIGH,
        description="Creates a device-mapper device. Verify parameters.",
        suggestions=(
            PatternSuggestion(command="dmsetup info", description="List existing devices first"),
            PatternSuggestion(command="losetup -a", description="Check loop devices if used by DM"),
        ),
    ),
    # ---- disk: nbd-client ----
    DestructivePattern(
        name="nbd-client-disconnect",
        regex=r"nbd-client\s+(?:.*\s+)?-d\b",
        reason="nbd-client -d disconnects a network block device.",
        severity=Severity.HIGH,
        description="Disconnects NBD device. Data loss if not unmounted.",
        suggestions=(
            PatternSuggestion(command="lsblk /dev/nbd{0}", description="Check if NBD device is mounted"),
            PatternSuggestion(command="mount | grep nbd", description="Check active mounts on NBD device"),
        ),
    ),
    DestructivePattern(
        name="nbd-client-connect",
        regex=r"nbd-client\s+\S+\s+\d+\s+['\"]?/dev/nbd",
        reason="nbd-client connects a network block device.",
        severity=Severity.HIGH,
        description="Connects NBD device. Verify server and device target.",
        suggestions=(
            PatternSuggestion(command="nbd-client -l {server}", description="List exports available on server"),
            PatternSuggestion(command="lsblk", description="Check local block devices before connecting"),
        ),
    ),
    # ---- disk: LVM ----
    DestructivePattern(
        name="pvremove",
        regex=r"\bpvremove\b",
        reason="pvremove ERASES LVM metadata from a physical volume.",
        severity=Severity.HIGH,
        description="Removes LVM PV. Data becomes inaccessible.",
        suggestions=(
            PatternSuggestion(command="pvs", description="List all PVs before removal"),
            PatternSuggestion(command="pvdisplay /dev/{sdc}", description="Check PV details before removing"),
        ),
    ),
    DestructivePattern(
        name="vgremove",
        regex=r"\bvgremove\b",
        reason="vgremove DELETES a volume group and all LVs within it.",
        severity=Severity.HIGH,
        description="Removes LVM VG and all logical volumes.",
        suggestions=(
            PatternSuggestion(command="vgs", description="List all VGs before removal"),
            PatternSuggestion(command="lvdisplay {vg}", description="Check LVs in VG before removal"),
        ),
    ),
    DestructivePattern(
        name="lvremove",
        regex=r"\blvremove\b",
        reason="lvremove PERMANENTLY deletes a logical volume and its data.",
        severity=Severity.HIGH,
        description="Deletes LVM LV and ALL data on it.",
        suggestions=(
            PatternSuggestion(command="lvs", description="List all LVs before removal"),
            PatternSuggestion(command="lvdisplay {vg}/{lv}", description="Check LV details before removing"),
        ),
    ),
    DestructivePattern(
        name="vgreduce",
        regex=r"\bvgreduce\b",
        reason="vgreduce removes a PV from a VG. Data may be lost.",
        severity=Severity.HIGH,
        description="Reduces VG by removing a PV.",
        suggestions=(
            PatternSuggestion(command="vgdisplay {vg}", description="Check VG details first"),
            PatternSuggestion(command="pvs -o+vg_name", description="Verify PV belongs to correct VG"),
        ),
    ),
    DestructivePattern(
        name="lvreduce",
        regex=r"\blvreduce\b",
        reason="lvreduce SHRINKS a logical volume. Data loss possible!",
        severity=Severity.HIGH,
        description="Shrinks LV. Data loss if FS not resized first!",
        suggestions=(
            PatternSuggestion(command="lvdisplay {vg}/{lv}", description="Check current LV size"),
            PatternSuggestion(command="df -h /dev/{vg}/{lv}", description="Check FS usage before shrinking"),
        ),
    ),
    DestructivePattern(
        name="lvresize-shrink",
        regex=r"lvresize\s+(?:.*\s+)?(?:-L\s*-|-l\s*-|--size\s+\S*-)",
        reason="lvresize with negative size SHRINKS the volume.",
        severity=Severity.HIGH,
        description="Shrinks LV via negative size. Resize FS first!",
        suggestions=(
            PatternSuggestion(command="lvdisplay {vg}/{lv}", description="Check current LV size"),
            PatternSuggestion(command="df -h /dev/{vg}/{lv}", description="Verify FS usage before shrinking"),
        ),
    ),
    DestructivePattern(
        name="pvmove",
        regex=r"\bpvmove\b",
        reason="pvmove migrates data between PVs. Do NOT interrupt!",
        severity=Severity.HIGH,
        description="Moves data between physical volumes. Interruption causes loss.",
        suggestions=(
            PatternSuggestion(command="pvs", description="Check PV allocation first"),
            PatternSuggestion(command="pvdisplay /dev/{sdc}", description="Check source PV details"),
        ),
    ),
    DestructivePattern(
        name="lvconvert-merge",
        regex=r"lvconvert\s+(?:.*\s+)?--merge\b",
        reason="lvconvert --merge reverts LV to snapshot state.",
        severity=Severity.HIGH,
        description="Merges LV snapshot, discarding changes since snapshot.",
        suggestions=(
            PatternSuggestion(command="lvdisplay {vg}/{lv}", description="Check snapshot and origin details"),
            PatternSuggestion(command="lvs -a", description="List all LVs including snapshots"),
        ),
    ),
)

SYSTEM_PERMISSIONS_PATTERNS: tuple[DestructivePattern, ...] = (
    # ---- permissions ----
    DestructivePattern(
        name="chmod-777",
        regex=r"chmod\s+(?:.*\s+)?[\\\"'=]?0*777(?:[\\\s\"']|$)",
        reason="chmod 777 makes files world-writable.",
        severity=Severity.HIGH,
        description="chmod 777 grants read/write/execute to everyone.",
        suggestions=(
            PatternSuggestion(command="chmod 755 {dir}", description="More restrictive permissions for directories"),
            PatternSuggestion(command='find {dir} -type f -exec chmod 644 {} \\;', description="Set files to 644, directories to 755"),
        ),
    ),
    DestructivePattern(
        name="chmod-recursive-root",
        regex=r"chmod\s+(?:.*(?:-[rR]|--recursive)).*\s+['\"]?/(?:$|bin|boot|dev|etc|lib|lib64|opt|proc|root|run|sbin|srv|sys|usr|var)\b",
        reason="chmod -R on system directories can break system permissions.",
        severity=Severity.CRITICAL,
        description="Recursive chmod on system dirs can break the system.",
        suggestions=(
            PatternSuggestion(command="chmod -R 755 {specific_dir}", description="Use a specific directory, not root"),
            PatternSuggestion(command='find {dir} -type f -exec chmod 644 {} \\;', description="Set files and dirs separately"),
        ),
    ),
    DestructivePattern(
        name="chown-recursive-root",
        regex=r"chown\s+(?:.*(?:-[rR]|--recursive)).*\s+['\"]?/(?:$|bin|boot|dev|etc|lib|lib64|opt|proc|root|run|sbin|srv|sys|usr|var)\b",
        reason="chown -R on system directories can break system ownership.",
        severity=Severity.HIGH,
        description="Recursive chown on system dirs can break services.",
        suggestions=(
            PatternSuggestion(command="chown -R {user}:{group} {specific_dir}", description="Use a specific directory, not root"),
            PatternSuggestion(command='find {dir} ! -user {user} -exec chown {user}:{group} {} \\;', description="Selective ownership change"),
        ),
    ),
    DestructivePattern(
        name="chmod-setuid",
        regex=r"chmod\s+.*u\+s|chmod\s+[4-7]\d{3}",
        reason="Setting setuid bit (chmod u+s) is security-sensitive.",
        severity=Severity.HIGH,
        description="setuid allows running with owner privileges.",
        suggestions=(
            PatternSuggestion(command="chmod u-s {file}", description="Remove setuid bit for safety"),
            PatternSuggestion(command="capsh --print", description="Check capabilities as an alternative"),
        ),
    ),
    DestructivePattern(
        name="chmod-setgid",
        regex=r"chmod\s+.*g\+s|chmod\s+[2367]\d{3}",
        reason="Setting setgid bit (chmod g+s) is security-sensitive.",
        severity=Severity.HIGH,
        description="setgid affects group privileges.",
        suggestions=(
            PatternSuggestion(command="chmod g-s {file}", description="Remove setgid bit for safety"),
            PatternSuggestion(command="getfacl {file}", description="Check ACLs as an alternative"),
        ),
    ),
    DestructivePattern(
        name="chown-to-root",
        regex=r"chown\s+.*root[:\s]",
        reason="Changing ownership to root should be done carefully.",
        severity=Severity.HIGH,
        description="chown to root makes files inaccessible to normal users.",
        suggestions=(
            PatternSuggestion(command="chown {user}:{group} {file}", description="Use a specific non-root user"),
            PatternSuggestion(command="sudo -u {user} {command}", description="Run as a specific user instead of chown"),
        ),
    ),
    DestructivePattern(
        name="setfacl-all",
        regex=r"setfacl\s+.*-[rR].*\s+['\"]?/(?:$|bin|boot|dev|etc|lib|lib64|opt|proc|root|run|sbin|srv|sys|usr|var)\b",
        reason="setfacl -R on system directories can modify access control across FS.",
        severity=Severity.CRITICAL,
        description="Recursive setfacl on system dirs breaks security boundaries.",
        suggestions=(
            PatternSuggestion(command="setfacl -R -m u:{user}:rwx {specific_dir}", description="Use a specific directory, not root"),
            PatternSuggestion(command="getfacl -R /{dir} | head -100", description="Preview current ACLs first"),
        ),
    ),
)

SYSTEM_SERVICES_PATTERNS: tuple[DestructivePattern, ...] = (
    # ---- services ----
    DestructivePattern(
        name="systemctl-stop-critical",
        regex=r"systemctl\b.*?\s+(?:stop|disable|mask)\s+(?:ssh|sshd|network|networking|firewalld|ufw|docker|containerd)\b",
        reason="Stopping/disabling critical services can cause access loss or outage.",
        severity=Severity.HIGH,
        description="Stop critical service: ssh, network, firewall, container runtime.",
        suggestions=(
            PatternSuggestion(command="systemctl status {service}", description="Check service status first"),
            PatternSuggestion(command="systemctl restart {service}", description="Restart instead of stop"),
        ),
    ),
    DestructivePattern(
        name="systemctl-stop",
        regex=r"systemctl\b.*?\s+(?:stop|disable|mask)\b",
        reason="systemctl stop/disable/mask affects service availability.",
        severity=Severity.HIGH,
        description="Stops, disables, or masks a systemd service.",
        suggestions=(
            PatternSuggestion(command="systemctl status {service}", description="Check service status first"),
            PatternSuggestion(command="systemctl restart {service}", description="Restart instead of stopping"),
        ),
    ),
    DestructivePattern(
        name="service-stop-critical",
        regex=r"service\s+(?:ssh|sshd|network|networking|docker)\s+stop",
        reason="Stopping critical services via service command can cause access loss.",
        severity=Severity.HIGH,
        description="Stops a critical service via SysV init.",
        suggestions=(
            PatternSuggestion(command="service {service} status", description="Check service status first"),
            PatternSuggestion(command="service {service} restart", description="Restart instead of stopping"),
        ),
    ),
    DestructivePattern(
        name="systemctl-isolate",
        regex=r"systemctl\b.*?\s+isolate\b",
        reason="systemctl isolate changes the system state significantly.",
        severity=Severity.HIGH,
        description="Isolates to a different systemd target.",
        suggestions=(
            PatternSuggestion(command="systemctl list-units --type=target", description="List available targets"),
            PatternSuggestion(command="systemctl get-default", description="Check current default target"),
        ),
    ),
    DestructivePattern(
        name="systemctl-power",
        regex=r"systemctl\b.*?\s+(?:poweroff|reboot|halt|suspend|hibernate)\b",
        reason="systemctl poweroff/reboot/halt shuts down or restarts the system.",
        severity=Severity.CRITICAL,
        description="System power state change: poweroff, reboot, halt, suspend.",
        suggestions=(
            PatternSuggestion(command='shutdown -r +5 "Scheduled restart"', description="Scheduled reboot with delay"),
            PatternSuggestion(command='wall "System will power off" && sleep 60 && shutdown -h now', description="Warn users before shutdown"),
        ),
    ),
    DestructivePattern(
        name="shutdown",
        regex=r"\bshutdown\b",
        reason="shutdown will power off or restart the system.",
        severity=Severity.CRITICAL,
        description="Shuts down or restarts the system.",
        suggestions=(
            PatternSuggestion(command='shutdown -r +5 "Scheduled restart"', description="Scheduled reboot with delay"),
            PatternSuggestion(command="shutdown -c", description="Cancel a scheduled shutdown"),
        ),
    ),
    DestructivePattern(
        name="reboot",
        regex=r"\breboot\b",
        reason="reboot will restart the system.",
        severity=Severity.CRITICAL,
        description="Restarts the system immediately.",
        suggestions=(
            PatternSuggestion(command='shutdown -r +5 "Scheduled restart"', description="Schedule reboot with delay instead of immediate"),
            PatternSuggestion(command='wall "System will restart" && sleep 60 && reboot', description="Warn users before restarting"),
        ),
    ),
    DestructivePattern(
        name="init-level",
        regex=r"\binit\s+[06]\b",
        reason="init 0 shuts down, init 6 reboots the system.",
        severity=Severity.CRITICAL,
        description="Init runlevel change to 0 (halt) or 6 (reboot).",
        suggestions=(
            PatternSuggestion(command='shutdown -r +5 "Scheduled restart"', description="Use shutdown with delay instead of init 6"),
            PatternSuggestion(command="systemctl isolate multi-user.target", description="Use systemctl isolate for non-destructive runlevel change"),
        ),
    ),
)

SYSTEM_PATTERNS: tuple[DestructivePattern, ...] = SYSTEM_DISK_PATTERNS + SYSTEM_PERMISSIONS_PATTERNS + SYSTEM_SERVICES_PATTERNS


def build_system_pack() -> Pack:
    return Pack(id="system", name="System patterns",
        destructive_patterns=SYSTEM_PATTERNS,
        keywords=("dd", "fdisk", "parted", "mkfs", "mkswap", "wipefs", "mount", "umount", "losetup", "mdadm", "btrfs", "dmsetup", "nbd-client", "pvremove", "vgremove", "lvremove", "vgreduce", "lvreduce", "lvresize", "pvmove", "lvconvert", "chmod", "chown", "setfacl", "systemctl", "service", "shutdown", "reboot", "init"),
    )
