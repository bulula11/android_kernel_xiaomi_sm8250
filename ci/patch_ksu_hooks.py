#!/usr/bin/env python3
"""给内核源码补上 BakaSU 手动钩子（非 GKI / 4.19）。

BakaSU 在编译 drivers/kernelsu 时会逐个 grep 这些符号，缺一个就直接
`*** You should integrate BakaSU in your kernel. Stop.`。它自带的
setup.sh 只做 drivers/Kconfig + Makefile 接线，不会改内核源码，所以
这几个钩子必须自己补。

用法：python3 ci/patch_ksu_hooks.py <kernel_source_dir>
"""
import os
import sys

EXEC_DECL = """#ifdef CONFIG_KSU_MANUAL_HOOK
extern __attribute__((hot)) int ksu_handle_execveat(int *fd, struct filename **filename_ptr,
						    void *argv, void *envp, int *flags);
extern __attribute__((hot)) int ksu_handle_post_execveat(int *fd, struct filename **filename_ptr,
							 void *argv, void *envp, int *flags, int *retval);
#endif

"""

EXEC_OLD = """static int do_execveat_common(int fd, struct filename *filename,
			      struct user_arg_ptr argv,
			      struct user_arg_ptr envp,
			      int flags)
{
	return __do_execve_file(fd, filename, argv, envp, flags, NULL);
}
"""

EXEC_NEW = """static int do_execveat_common(int fd, struct filename *filename,
			      struct user_arg_ptr argv,
			      struct user_arg_ptr envp,
			      int flags)
{
#ifdef CONFIG_KSU_MANUAL_HOOK
	int retval;

	ksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);
	retval = __do_execve_file(fd, filename, argv, envp, flags, NULL);
	ksu_handle_post_execveat(&fd, &filename, &argv, &envp, &flags, &retval);
	return retval;
#else
	return __do_execve_file(fd, filename, argv, envp, flags, NULL);
#endif
}
"""

OPEN_OLD = """SYSCALL_DEFINE3(faccessat, int, dfd, const char __user *, filename, int, mode)
{
	return do_faccessat(dfd, filename, mode);
}
"""

OPEN_NEW = """#ifdef CONFIG_KSU_MANUAL_HOOK
extern __attribute__((hot)) int ksu_handle_faccessat(int *dfd, const char __user **filename_user,
						     int *mode, int *flags);
#endif

SYSCALL_DEFINE3(faccessat, int, dfd, const char __user *, filename, int, mode)
{
#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_faccessat(&dfd, &filename, &mode, NULL);
#endif
	return do_faccessat(dfd, filename, mode);
}
"""

STAT_DECL_ANCHOR = """#if !defined(__ARCH_WANT_STAT64) || defined(__ARCH_WANT_SYS_NEWFSTATAT)
SYSCALL_DEFINE4(newfstatat, int, dfd, const char __user *, filename,
"""

STAT_DECL_NEW = """#ifdef CONFIG_KSU_MANUAL_HOOK
extern __attribute__((hot)) int ksu_handle_stat(int *dfd, const char __user **filename_user,
						int *flags);
extern void ksu_handle_newfstat_ret(unsigned int *fd, struct stat __user **statbuf_ptr);
#if defined(__ARCH_WANT_STAT64) || defined(__ARCH_WANT_COMPAT_STAT64)
extern void ksu_handle_fstat64_ret(unsigned long *fd, struct stat64 __user **statbuf_ptr);
#endif
#endif

""" + STAT_DECL_ANCHOR

NEWFSTATAT_OLD = """SYSCALL_DEFINE4(newfstatat, int, dfd, const char __user *, filename,
		struct stat __user *, statbuf, int, flag)
{
	struct kstat stat;
	int error;

	error = vfs_fstatat(dfd, filename, &stat, flag);
"""

NEWFSTATAT_NEW = """SYSCALL_DEFINE4(newfstatat, int, dfd, const char __user *, filename,
		struct stat __user *, statbuf, int, flag)
{
	struct kstat stat;
	int error;

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_stat(&dfd, &filename, &flag);
#endif
	error = vfs_fstatat(dfd, filename, &stat, flag);
"""

FSTATAT64_OLD = """SYSCALL_DEFINE4(fstatat64, int, dfd, const char __user *, filename,
		struct stat64 __user *, statbuf, int, flag)
{
	struct kstat stat;
	int error;

	error = vfs_fstatat(dfd, filename, &stat, flag);
"""

FSTATAT64_NEW = """SYSCALL_DEFINE4(fstatat64, int, dfd, const char __user *, filename,
		struct stat64 __user *, statbuf, int, flag)
{
	struct kstat stat;
	int error;

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_stat(&dfd, &filename, &flag);
#endif
	error = vfs_fstatat(dfd, filename, &stat, flag);
"""

NEWFSTAT_OLD = """SYSCALL_DEFINE2(newfstat, unsigned int, fd, struct stat __user *, statbuf)
{
	struct kstat stat;
	int error = vfs_fstat(fd, &stat);

	if (!error)
		error = cp_new_stat(&stat, statbuf);

	return error;
}
"""

NEWFSTAT_NEW = """SYSCALL_DEFINE2(newfstat, unsigned int, fd, struct stat __user *, statbuf)
{
	struct kstat stat;
	int error = vfs_fstat(fd, &stat);

	if (!error)
		error = cp_new_stat(&stat, statbuf);

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_newfstat_ret(&fd, &statbuf);
#endif
	return error;
}
"""

FSTAT64_OLD = """SYSCALL_DEFINE2(fstat64, unsigned long, fd, struct stat64 __user *, statbuf)
{
	struct kstat stat;
	int error = vfs_fstat(fd, &stat);

	if (!error)
		error = cp_new_stat64(&stat, statbuf);

	return error;
}
"""

FSTAT64_NEW = """SYSCALL_DEFINE2(fstat64, unsigned long, fd, struct stat64 __user *, statbuf)
{
	struct kstat stat;
	int error = vfs_fstat(fd, &stat);

	if (!error)
		error = cp_new_stat64(&stat, statbuf);

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_fstat64_ret(&fd, &statbuf);
#endif
	return error;
}
"""

REBOOT_OLD = """SYSCALL_DEFINE4(reboot, int, magic1, int, magic2, unsigned int, cmd,
		void __user *, arg)
{
	struct pid_namespace *pid_ns = task_active_pid_ns(current);
	char buffer[256];
	int ret = 0;

"""

REBOOT_NEW = """#ifdef CONFIG_KSU_MANUAL_HOOK
extern int ksu_handle_sys_reboot(int magic1, int magic2, unsigned int cmd, void __user **arg);
#endif

SYSCALL_DEFINE4(reboot, int, magic1, int, magic2, unsigned int, cmd,
		void __user *, arg)
{
	struct pid_namespace *pid_ns = task_active_pid_ns(current);
	char buffer[256];
	int ret = 0;

#ifdef CONFIG_KSU_MANUAL_HOOK
	ksu_handle_sys_reboot(magic1, magic2, cmd, &arg);
#endif
"""


def patch(root, rel, marker, pairs):
    path = os.path.join(root, rel)
    with open(path, encoding='utf-8', errors='surrogateescape') as f:
        text = f.read()
    if marker in text:
        print('[=] %-16s already patched' % rel)
        return True
    for old, new in pairs:
        if old not in text:
            print('[x] %-16s anchor not found:\n%s' % (rel, old[:160]))
            return False
        if text.count(old) != 1:
            print('[x] %-16s anchor appears %d times' % (rel, text.count(old)))
            return False
        text = text.replace(old, new, 1)
    with open(path, 'w', encoding='utf-8', newline='\n', errors='surrogateescape') as f:
        f.write(text)
    print('[+] %-16s patched' % rel)
    return True


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = sys.argv[1]
    ok = True
    ok &= patch(root, 'fs/exec.c', 'ksu_handle_post_execveat',
                [(EXEC_OLD, EXEC_DECL + EXEC_NEW)])
    ok &= patch(root, 'fs/open.c', 'ksu_handle_faccessat',
                [(OPEN_OLD, OPEN_NEW)])
    ok &= patch(root, 'fs/stat.c', 'ksu_handle_newfstat_ret',
                [(STAT_DECL_ANCHOR, STAT_DECL_NEW),
                 (NEWFSTATAT_OLD, NEWFSTATAT_NEW),
                 (FSTATAT64_OLD, FSTATAT64_NEW),
                 (NEWFSTAT_OLD, NEWFSTAT_NEW),
                 (FSTAT64_OLD, FSTAT64_NEW)])
    ok &= patch(root, 'kernel/reboot.c', 'ksu_handle_sys_reboot',
                [(REBOOT_OLD, REBOOT_NEW)])
    if not ok:
        print('[-] some hooks could not be applied')
        return 1
    print('[+] all BakaSU manual hooks applied')
    return 0


if __name__ == '__main__':
    sys.exit(main())
