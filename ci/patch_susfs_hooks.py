#!/usr/bin/env python3
"""为 BakaSU 的 SUSFS Inline Hook 模式注入 KSU syscall 钩子。

背景：
  BakaSU 的 Kconfig 里 KSU_TRACEPOINT_HOOK / KSU_MANUAL_HOOK / KSU_SUSFS 是
  同一个 choice 的三个互斥选项。一旦打开 CONFIG_KSU_SUSFS，钩子方式就变成
  "SUSFS Inline Hook"，此时 tools/inline_hook_check.mk 会强制检查内核里是否
  存在下面这些 ksu_handle_* 调用点（缺一个就 Stop）：

    kernel/sys.c          ksu_handle_setresuid
    fs/exec.c             ksu_handle_execveat (+ sucompat / post_execveat)
    fs/open.c             ksu_handle_faccessat
    fs/read_write.c       ksu_handle_sys_read
    fs/stat.c             ksu_handle_stat / ksu_handle_vfs_fstat
    kernel/reboot.c       ksu_handle_sys_reboot
    drivers/input/input.c ksu_handle_input_handle_event

  这些调用点不在 SUSFS 的内核补丁里，需要按 BakaSU 的约定手工插入。
  内容对齐 9Qie/android_kernel_xiaomi_sm8250 @ bakasu-cas（SUSFS v2.3.0 +
  BakaSU 的已验证组合），只取 KSU 钩子部分，不携带该树的其它本地改动。

用法：python3 patch_susfs_hooks.py <内核源码根目录>
"""

import os
import sys


def read(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def sub(root, rel, old, new, label, marker):
    """精确替换；marker 已存在则视为已打补丁。"""
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        sys.exit(f"[x] {label}: 文件不存在 {rel}")
    src = read(path)
    if marker in src:
        print(f"[=] {label}: 已存在，跳过")
        return
    n = src.count(old)
    if n != 1:
        sys.exit(f"[x] {label}: 锚点匹配 {n} 次（应为 1），{rel} 结构与预期不符")
    write(path, src.replace(old, new, 1))
    print(f"[+] {label}")


# ---------------------------------------------------------------- fs/exec.c
def patch_exec(root):
    sub(root, "fs/exec.c",
        "#include <linux/vmalloc.h>\n",
        "#include <linux/vmalloc.h>\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "#include <linux/susfs_def.h>\n"
        "#endif\n",
        "exec.c include", "susfs_def.h")

    sub(root, "fs/exec.c",
        "static int __do_execve_file(int fd, struct filename *filename,",
        "#ifdef CONFIG_KSU_SUSFS\n"
        "extern struct static_key_true ksu_su_compat_enabled;\n"
        "extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;\n"
        "extern bool __ksu_is_allow_uid_for_current(uid_t uid);\n"
        "extern int ksu_handle_execveat(int *fd, struct filename **filename_ptr, void *argv,\n"
        "\t\t\tvoid *envp, int *flags);\n"
        "extern int ksu_handle_execveat_sucompat(int *fd, struct filename **filename_ptr, void *argv,\n"
        "\t\t\t\tvoid *envp, int *flags);\n"
        "extern int ksu_handle_post_execveat_sucompat(int *fd, struct filename **filename_ptr, void *argv,\n"
        "\t\t\tvoid *envp, int *flags, int *retval);\n"
        "#endif\n"
        "static int __do_execve_file(int fd, struct filename *filename,",
        "exec.c externs", "ksu_handle_post_execveat_sucompat(int")

    sub(root, "fs/exec.c",
        "\tstruct files_struct *displaced;\n"
        "\tint retval;\n"
        "\n"
        "\tif (IS_ERR(filename))\n"
        "\t\treturn PTR_ERR(filename);\n",
        "\tstruct files_struct *displaced;\n"
        "\tint retval;\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tbool is_su_session = false;\n"
        "#endif\n"
        "\n"
        "\tif (IS_ERR(filename))\n"
        "\t\treturn PTR_ERR(filename);\n"
        "\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\t/* 走到这里说明当前进程不是 su 会话，直接走原生流程 */\n"
        "\tif (likely(susfs_is_current_proc_no_su()))\n"
        "\t\tgoto orig_flow;\n"
        "\tif (static_branch_likely(&ksu_su_compat_enabled)) {\n"
        "\t\tif (static_branch_unlikely(&susfs_is_sdcard_android_data_not_decrypted))\n"
        "\t\t\tis_su_session = !ksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);\n"
        "\t\telse\n"
        "\t\t\tis_su_session = !ksu_handle_execveat_sucompat(&fd, &filename, &argv, &envp, &flags);\n"
        "\t}\n"
        "orig_flow:\n"
        "#endif\n",
        "exec.c execveat hook", "is_su_session = false;")

    sub(root, "fs/exec.c",
        "out_unmark:\n"
        "\tcurrent->fs->in_exec = 0;\n"
        "\tcurrent->in_execve = 0;\n"
        "\n"
        "out_free:\n",
        "out_unmark:\n"
        "\tcurrent->fs->in_exec = 0;\n"
        "\tcurrent->in_execve = 0;\n"
        "\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tif (unlikely(is_su_session))\n"
        "\t\t(void)ksu_handle_post_execveat_sucompat(&fd, &filename, &argv, &envp, &flags, &retval);\n"
        "#endif\n"
        "\n"
        "out_free:\n",
        "exec.c post_execveat hook", "ksu_handle_post_execveat_sucompat(&fd")


# ---------------------------------------------------------------- fs/open.c
def patch_open(root):
    sub(root, "fs/open.c",
        "#include <linux/compat.h>\n",
        "#include <linux/compat.h>\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "#include <linux/susfs_def.h>\n"
        "#endif\n",
        "open.c include", "susfs_def.h")

    sub(root, "fs/open.c",
        "long do_faccessat(int dfd, const char __user *filename, int mode)\n{\n",
        "#ifdef CONFIG_KSU_SUSFS\n"
        "extern struct static_key_true ksu_su_compat_enabled;\n"
        "extern bool __ksu_is_allow_uid_for_current(uid_t uid);\n"
        "extern int ksu_handle_faccessat(int *dfd, struct filename **filename, int *mode,\n"
        "\t\t\tint *flags);\n"
        "#endif\n"
        "long do_faccessat(int dfd, const char __user *filename, int mode)\n{\n",
        "open.c externs", "ksu_handle_faccessat(int")

    sub(root, "fs/open.c",
        "\tint res;\n"
        "\tunsigned int lookup_flags = LOOKUP_FOLLOW;\n"
        "\n"
        "\tif (mode & ~S_IRWXO)\t/* where's F_OK, X_OK, W_OK, R_OK? */\n",
        "\tint res;\n"
        "\tunsigned int lookup_flags = LOOKUP_FOLLOW;\n"
        "\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tstruct filename *fname = NULL;\n"
        "#endif\n"
        "\n"
        "\tif (mode & ~S_IRWXO)\t/* where's F_OK, X_OK, W_OK, R_OK? */\n",
        "open.c fname decl", "struct filename *fname = NULL;")

    sub(root, "fs/open.c",
        "retry:\n"
        "\tres = user_path_at(dfd, filename, lookup_flags, &path);\n",
        "retry:\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tfname = getname_flags(filename, lookup_flags, NULL);\n"
        "\n"
        "\tif (likely(susfs_is_current_proc_no_su()))\n"
        "\t\tgoto orig_flow;\n"
        "\n"
        "\tif (static_branch_likely(&ksu_su_compat_enabled)) {\n"
        "\t\tif (unlikely(__ksu_is_allow_uid_for_current(current_uid().val)))\n"
        "\t\t\tksu_handle_faccessat(&dfd, &fname, &mode, NULL);\n"
        "\t}\n"
        "\n"
        "orig_flow:\n"
        "\t/* filename_lookup() 内部会负责 putname()，此处无需再释放 */\n"
        "\tres = filename_lookup(dfd, fname, lookup_flags, &path, NULL);\n"
        "#else\n"
        "\tres = user_path_at(dfd, filename, lookup_flags, &path);\n"
        "#endif\n",
        "open.c faccessat hook", "ksu_handle_faccessat(&dfd")


# ----------------------------------------------------------- fs/read_write.c
def patch_read_write(root):
    sub(root, "fs/read_write.c",
        "SYSCALL_DEFINE3(read, unsigned int, fd, char __user *, buf, size_t, count)\n"
        "{\n"
        "\treturn ksys_read(fd, buf, count);\n"
        "}\n",
        "#ifdef CONFIG_KSU_SUSFS\n"
        "extern struct static_key_true ksu_is_init_rc_hook_enabled;\n"
        "extern __attribute__((cold)) int ksu_handle_sys_read(unsigned int fd, char __user **buf_ptr, size_t *count_ptr);\n"
        "#endif\n"
        "SYSCALL_DEFINE3(read, unsigned int, fd, char __user *, buf, size_t, count)\n"
        "{\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tif (static_branch_unlikely(&ksu_is_init_rc_hook_enabled))\n"
        "\t\tksu_handle_sys_read(fd, &buf, &count);\n"
        "#endif\n"
        "\treturn ksys_read(fd, buf, count);\n"
        "}\n",
        "read_write.c sys_read hook", "ksu_handle_sys_read(fd, &buf, &count)")


# ---------------------------------------------------------------- kernel/sys.c
def patch_sys(root):
    sub(root, "kernel/sys.c",
        "SYSCALL_DEFINE3(setresuid, uid_t, ruid, uid_t, euid, uid_t, suid)\n"
        "{\n"
        "\treturn __sys_setresuid(ruid, euid, suid);\n"
        "}\n",
        "#ifdef CONFIG_KSU_SUSFS\n"
        "extern int ksu_handle_setresuid(uid_t ruid, uid_t euid, uid_t suid);\n"
        "#endif\n"
        "\n"
        "SYSCALL_DEFINE3(setresuid, uid_t, ruid, uid_t, euid, uid_t, suid)\n"
        "{\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tif (ksu_handle_setresuid(ruid, euid, suid))\n"
        "\t\tpr_info(\"ksu_handle_setresuid() returned non-zero\\n\");\n"
        "#endif\n"
        "\n"
        "\treturn __sys_setresuid(ruid, euid, suid);\n"
        "}\n",
        "sys.c setresuid hook", "ksu_handle_setresuid(ruid")


# -------------------------------------------------------------- kernel/reboot.c
def patch_reboot(root):
    sub(root, "kernel/reboot.c",
        "SYSCALL_DEFINE4(reboot, int, magic1, int, magic2, unsigned int, cmd,\n"
        "\t\tvoid __user *, arg)\n"
        "{\n"
        "\tstruct pid_namespace *pid_ns = task_active_pid_ns(current);\n"
        "\tchar buffer[256];\n"
        "\tint ret = 0;\n",
        "#ifdef CONFIG_KSU_SUSFS\n"
        "extern int ksu_handle_sys_reboot(int magic1, int magic2, unsigned int cmd, void __user **arg);\n"
        "#endif\n"
        "\n"
        "SYSCALL_DEFINE4(reboot, int, magic1, int, magic2, unsigned int, cmd,\n"
        "\t\tvoid __user *, arg)\n"
        "{\n"
        "\tstruct pid_namespace *pid_ns = task_active_pid_ns(current);\n"
        "\tchar buffer[256];\n"
        "\tint ret = 0;\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tif (system_state == SYSTEM_RUNNING)\n"
        "\t\tksu_handle_sys_reboot(magic1, magic2, cmd, &arg);\n"
        "#endif\n",
        "reboot.c sys_reboot hook", "ksu_handle_sys_reboot(magic1")


# ------------------------------------------------------- drivers/input/input.c
def patch_input(root):
    sub(root, "drivers/input/input.c",
        "static void input_handle_event(struct input_dev *dev,\n"
        "\t\t\t       unsigned int type, unsigned int code, int value)\n"
        "{\n"
        "\tint disposition = input_get_disposition(dev, type, code, &value);\n",
        "#ifdef CONFIG_KSU_SUSFS\n"
        "extern struct static_key_true ksu_is_input_hook_enabled;\n"
        "extern int ksu_handle_input_handle_event(unsigned int *type, unsigned int *code, int *value);\n"
        "#endif\n"
        "\n"
        "static void input_handle_event(struct input_dev *dev,\n"
        "\t\t\t       unsigned int type, unsigned int code, int value)\n"
        "{\n"
        "\tint disposition = input_get_disposition(dev, type, code, &value);\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tif (static_branch_unlikely(&ksu_is_input_hook_enabled))\n"
        "\t\tksu_handle_input_handle_event(&type, &code, &value);\n"
        "#endif\n",
        "input.c input hook", "ksu_handle_input_handle_event(&type")


# ---------------------------------------------------------------- fs/stat.c
def patch_stat(root):
    # filename_lookup() 的声明只在 fs/internal.h 里（不是公开头文件），
    # 不加这个 include 会 -Werror=implicit-function-declaration 直接编译失败
    sub(root, "fs/stat.c",
        "#include <linux/compat.h>\n"
        "\n"
        "#include <linux/uaccess.h>\n",
        "#include <linux/compat.h>\n"
        "#include \"internal.h\"\n"
        "\n"
        "#include <linux/uaccess.h>\n",
        "stat.c internal.h", "#include \"internal.h\"")

    sub(root, "fs/stat.c",
        "#include <asm/unistd.h>\n\n"
        "#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT\n",
        "#include <asm/unistd.h>\n\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "#include <linux/susfs_def.h>\n"
        "extern struct static_key_true ksu_is_init_rc_hook_enabled;\n"
        "extern void ksu_handle_vfs_fstat(int fd, loff_t *kstat_size_ptr);\n"
        "extern struct static_key_true ksu_su_compat_enabled;\n"
        "extern bool __ksu_is_allow_uid_for_current(uid_t uid);\n"
        "extern int ksu_handle_stat(int *dfd, struct filename **filename, int *flags);\n"
        "#endif // #ifdef CONFIG_KSU_SUSFS\n\n"
        "#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT\n",
        "stat.c include+externs", "ksu_handle_vfs_fstat(int")

    sub(root, "fs/stat.c",
        "\tf = fdget_raw(fd);\n"
        "\tif (f.file) {\n"
        "\t\terror = vfs_getattr(&f.file->f_path, stat,\n"
        "\t\t\t\t    request_mask, query_flags);\n"
        "\t\tfdput(f);\n"
        "\t}\n",
        "\tf = fdget_raw(fd);\n"
        "\tif (f.file) {\n"
        "\t\terror = vfs_getattr(&f.file->f_path, stat,\n"
        "\t\t\t\t    request_mask, query_flags);\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\t\tif (static_branch_unlikely(&ksu_is_init_rc_hook_enabled))\n"
        "\t\t\tksu_handle_vfs_fstat(fd, &stat->size);\n"
        "#endif // #ifdef CONFIG_KSU_SUSFS\n"
        "\t\tfdput(f);\n"
        "\t}\n",
        "stat.c vfs_fstat hook", "ksu_handle_vfs_fstat(fd")

    sub(root, "fs/stat.c",
        "\tint error = -EINVAL;\n"
        "\tunsigned int lookup_flags = LOOKUP_FOLLOW | LOOKUP_AUTOMOUNT;\n"
        "\n"
        "\tif ((flags & ~(AT_SYMLINK_NOFOLLOW | AT_NO_AUTOMOUNT |\n",
        "\tint error = -EINVAL;\n"
        "\tunsigned int lookup_flags = LOOKUP_FOLLOW | LOOKUP_AUTOMOUNT;\n"
        "\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tstruct filename *fname = NULL;\n"
        "#endif\n"
        "\n"
        "\tif ((flags & ~(AT_SYMLINK_NOFOLLOW | AT_NO_AUTOMOUNT |\n",
        "stat.c fname decl", "struct filename *fname = NULL;")

    sub(root, "fs/stat.c",
        "retry:\n"
        "\terror = user_path_at(dfd, filename, lookup_flags, &path);\n",
        "retry:\n"
        "#ifdef CONFIG_KSU_SUSFS\n"
        "\tfname = getname_flags(filename, lookup_flags, NULL);\n"
        "\n"
        "\tif (likely(susfs_is_current_proc_no_su()))\n"
        "\t\tgoto orig_flow;\n"
        "\n"
        "\tif (static_branch_likely(&ksu_su_compat_enabled)) {\n"
        "\t\tif (unlikely(__ksu_is_allow_uid_for_current(current_uid().val)))\n"
        "\t\t\tksu_handle_stat(&dfd, &fname, &flags);\n"
        "\t}\n"
        "\n"
        "orig_flow:\n"
        "\terror = filename_lookup(dfd, fname, lookup_flags, &path, NULL);\n"
        "#else\n"
        "\terror = user_path_at(dfd, filename, lookup_flags, &path);\n"
        "#endif\n",
        "stat.c stat hook", "ksu_handle_stat(&dfd")


CHECKS = [
    ("fs/exec.c", "ksu_handle_execveat(&fd"),
    ("fs/exec.c", "ksu_handle_post_execveat_sucompat(&fd"),
    ("fs/open.c", "ksu_handle_faccessat(&dfd"),
    ("fs/read_write.c", "ksu_handle_sys_read(fd, &buf"),
    ("kernel/sys.c", "ksu_handle_setresuid(ruid"),
    ("kernel/reboot.c", "ksu_handle_sys_reboot(magic1"),
    ("drivers/input/input.c", "ksu_handle_input_handle_event(&type"),
    ("fs/stat.c", "ksu_handle_stat(&dfd"),
    ("fs/stat.c", "ksu_handle_vfs_fstat(fd"),
]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    if not os.path.isdir(os.path.join(root, "fs")):
        sys.exit(f"[x] {root} 不像内核源码根目录")

    patch_exec(root)
    patch_open(root)
    patch_read_write(root)
    patch_sys(root)
    patch_reboot(root)
    patch_input(root)
    patch_stat(root)

    print("\n=== 钩子核对 ===")
    bad = 0
    for rel, needle in CHECKS:
        ok = needle in read(os.path.join(root, rel))
        print(f"  {'OK  ' if ok else 'MISS'} {rel}: {needle}")
        bad += 0 if ok else 1
    if bad:
        sys.exit(f"[x] 有 {bad} 个钩子缺失")


if __name__ == "__main__":
    main()
