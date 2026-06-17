import os
import shutil

from metadata_moving import remove_empty_folders

def directory_transfer():
    main_dirs = [
        r"D:\Soulseek lossless\complete",
       # r"D:\Music\Nicotine Music"
    ]

    target_dir = r"D:\Lossless Music"

    os.makedirs(target_dir, exist_ok=True)

    for main_dir in main_dirs:
        for dirpath, _, filenames in os.walk(main_dir):
            for filename in filenames:
                src = os.path.join(dirpath, filename)
                dst = os.path.join(target_dir, filename)
                shutil.move(src, dst)

        remove_empty_folders(main_dir)             
        
if __name__ == "__main__":
    directory_transfer()