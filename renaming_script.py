import os
import shutil


def rename_and_copy_images(source_folder, dest_folder):
    """
    Copies images from the source folder to the destination folder with renamed filenames
    by removing ".nii.gz" from the file names.

    :param source_folder: Path to the folder containing the original images.
    :param dest_folder: Path to the folder where the renamed images will be saved.
    """
    try:
        # Create the destination folder if it doesn't exist
        if not os.path.exists(dest_folder):
            os.makedirs(dest_folder)
            print(f"Destination directory created at: {dest_folder}")
        else:
            print(f"Destination directory already exists at: {dest_folder}")

        # Iterate over each file in the source folder
        for filename in os.listdir(source_folder):
            if filename.endswith(".png"):  # Only process .png files
                # Remove ".nii.gz" from the filename
                new_filename = filename.replace(".nii.gz", "")

                # Build full file paths
                src_file = os.path.join(source_folder, filename)
                dest_file = os.path.join(dest_folder, new_filename)

                # Copy and rename the file
                shutil.copy(src_file, dest_file)
                # print(f"Copied and renamed: {filename} -> {new_filename}")

    except Exception as e:
        print(f"An error occurred: {e}")


# Example usage:
source_folder = 'test_preds/predictions_2d'  # Folder with images like Patient_42.nii.gz_0000.png
dest_folder = 'test_preds/predictions_2d_final'  # Folder where renamed images will be copied

rename_and_copy_images(source_folder, dest_folder)