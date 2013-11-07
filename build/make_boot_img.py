#!/usr/bin/python3

import argparse
import sys
import os
import subprocess
import shutil
import re
import stat
import uuid
import logging

from hashlib import md5

import pld_nr_buildconf

logger = logging.getLogger()


CYLINDER=8225280

DU_OUTPUT_RE = re.compile("^(\d+)\s+total", re.MULTILINE)

def copy_dir(source, dest):
    os.chdir(source)
    for dirpath, dirnames, filenames in os.walk("."):
        dirpath = dirpath[2:] # strip "./"
        for dirname in dirnames:
            path = os.path.join(dirpath, dirname)
            dst_path = os.path.join(dest, path)
            if not os.path.exists(dst_path):
                os.makedirs(dst_path)
        for filename in filenames:
            if filename.endswith("~"):
                continue
            path = os.path.join(dirpath, filename)
            dst_path = os.path.join(dest, path)
            shutil.copy(path, dst_path)

def install_grub(platform, lodev, boot_mnt_dir, grub_prefix, grub_early_fn):
    grub_prefix_dir = os.path.join(boot_mnt_dir, grub_prefix)
    grub_plat_dir = os.path.join(grub_prefix_dir, platform)
    if not os.path.exists(grub_plat_dir):
        os.makedirs(grub_plat_dir)
    if platform.endswith("-efi"):
        efi_dir = os.path.join(boot_mnt_dir, "EFI/BOOT")
        if not os.path.exists(efi_dir):
            os.makedirs(efi_dir)
        if "64" in platform:
            grub_img_fn = os.path.join(efi_dir, "BOOTX64.EFI")
        else:
            grub_img_fn = os.path.join(efi_dir, "BOOTIA32.EFI")
    else:
        grub_img_fn = os.path.join(grub_plat_dir, "core.img")

    grub_core_modules = ["search", "search_label", "fat", "part_msdos"]
    if platform.endswith("-pc"):
        grub_core_modules += ["biosdisk"]
    subprocess.check_call(["grub-mkimage",
                            "--output", grub_img_fn,
                            "--format", platform,
                            "--prefix", "/grub",
                            "--config", grub_early_fn,
                            ] + grub_core_modules)
    if platform.endswith("-pc"):
        shutil.copy("/lib/grub/{0}/boot.img".format(platform),
                                os.path.join(grub_plat_dir, "boot.img"))
        subprocess.check_call(["grub-bios-setup",
                                "--directory", grub_plat_dir,
                                lodev])
    copy_dir("/lib/grub/{0}".format(platform), grub_plat_dir)

def main():
    log_parser = pld_nr_buildconf.get_logging_args_parser()
    parser = argparse.ArgumentParser(description="Make boot image",
                                     parents=[log_parser])
    parser.add_argument("destination",
                        help="Destination file name")
    args = parser.parse_args()
    pld_nr_buildconf.setup_logging(args)
    
    config = pld_nr_buildconf.Config.get_config()

    boot_img_fn = os.path.abspath(args.destination)
    root_dir = os.path.abspath("root")
    boot_img_dir = os.path.abspath("../boot_img")
    init_cpio_fn = os.path.abspath("init.cpi")
    vmlinuz_fn = os.path.abspath("root/boot/vmlinuz")
    boot_mnt_dir = os.path.abspath("boot_mnt")
    if not os.path.isdir(boot_mnt_dir):
        os.makedirs(boot_mnt_dir)
    grub_early_fn = os.path.abspath("grub_early.cfg")
    grub_prefix = "grub"

    module_files = []
    for module in config.modules:
        module_files.append(os.path.abspath("{0}.cpi".format(module)))

    if os.path.exists("uuid"):
        with open("uuid", "rt") as uuid_f:
            img_uuid = uuid.UUID(uuid_f.read().strip())
    else:
        img_uuid = uuid.uuid4()
        with open("uuid", "wt") as uuid_f:
            print(str(img_uuid), file=uuid_f)
    boot_vol_id = md5(img_uuid.bytes).hexdigest()[:8]

    with open(grub_early_fn, "wt") as grub_early:
        grub_early.write("search.fs_uuid {0}-{1} root\nset prefix=($root)/grub\n"
                .format(boot_vol_id[:4], boot_vol_id[4:]))

    du_output = subprocess.check_output(["du", "-sbcD",
                                            "/lib/grub",
                                            boot_img_dir,
                                            init_cpio_fn,
                                            vmlinuz_fn,
                                            ] + module_files)
    match = DU_OUTPUT_RE.search(du_output.decode("utf-8"))
    bytes_needed = int(int(match.group(1)) * 1.1)
    logger.debug("bytes needed: {0!r}".format(bytes_needed))
    cylinders_needed = max(bytes_needed // CYLINDER + 2, 2)
    logger.debug("cylinders needed: {0!r}".format(cylinders_needed))

    subprocess.check_call(["dd", "if=/dev/zero", "of=" + boot_img_fn,
                            "bs={0}".format(CYLINDER),
                            "count={0}".format(cylinders_needed)])
    try:
        lodev = subprocess.check_output(["losetup", "--partscan", "--find",
                                                        "--show", boot_img_fn])
        lodev = lodev.decode("utf-8").strip()
        try:
            sfdisk_p = subprocess.Popen(["sfdisk", lodev],
                                        stdin=subprocess.PIPE)
            sfdisk_p.communicate(b"1,+,e,*\n0,0,0\n0,0,0\n0,0,0\n")
            rc = sfdisk_p.wait()
            if rc:
                raise subprocess.CalledProcessError(rc, ["sfdisk"])
            subprocess.check_call(["mkdosfs", "-F", "16", "-I",
                                        "-i", boot_vol_id, lodev + "p1"])
            subprocess.check_call(["mount", "-t", "vfat", lodev + "p1",
                                        boot_mnt_dir])
            try:
                shutil.copy(vmlinuz_fn,
                            os.path.join(boot_mnt_dir, "vmlinuz"))
                shutil.copy(init_cpio_fn,
                            os.path.join(boot_mnt_dir, "init.cpi"))
                for module_f in module_files:
                    module_fn = os.path.basename(module_f)
                    shutil.copy(module_f,
                            os.path.join(boot_mnt_dir, module_fn))
                copy_dir(boot_img_dir, boot_mnt_dir)
                for platform in config.grub_platforms:
                    install_grub(platform, lodev, boot_mnt_dir, grub_prefix,
                                                                grub_early_fn)
            finally:
                subprocess.call(["umount", boot_mnt_dir])
        finally:
            subprocess.call(["losetup", "-d", lodev])
    except:
        os.unlink(boot_img_fn)
        raise

if __name__ == "__main__":
    main()

# vi: sts=4 sw=4 et
