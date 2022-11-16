#!/usr/bin/env python
# pylint: disable=too-many-lines
"""Processes a video or image sequence to a nerfstudio compatible dataset."""

import json
import shutil
import subprocess
import sys
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple, Union

import appdirs
import numpy as np
import requests
import tyro
import yaml
from PIL import Image
from rich.console import Console
from rich.progress import track
from scipy.spatial.transform import Rotation
from typing_extensions import Annotated, Literal

from nerfstudio.utils import colmap_utils, install_checks, io

CONSOLE = Console(width=120)


class CameraModel(Enum):
    """Enum for camera types."""

    OPENCV = "OPENCV"
    OPENCV_FISHEYE = "OPENCV_FISHEYE"
    EQUIRECTANGULAR = "EQUIRECTANGULAR"


CAMERA_MODELS = {
    "perspective": CameraModel.OPENCV,
    "fisheye": CameraModel.OPENCV_FISHEYE,
    "equirectangular": CameraModel.EQUIRECTANGULAR,
}


def status(msg: str, spinner: str = "bouncingBall", verbose: bool = False):
    """A context manager that does nothing is verbose is True. Otherwise it hides logs under a message.

    Args:
        msg: The message to log.
        spinner: The spinner to use.
        verbose: If True, print all logs, else hide them.
    """
    if verbose:
        return nullcontext()
    return CONSOLE.status(msg, spinner=spinner)


def get_colmap_version(colmap_cmd: str, default_version=3.8) -> float:
    """Returns the version of COLMAP.
    This code assumes that colmap returns a version string of the form
    "COLMAP 3.8 ..." which may not be true for all versions of COLMAP.

    Args:
        default_version: Default version to return if COLMAP version can't be determined.
    Returns:
        The version of COLMAP.
    """
    output = run_command(colmap_cmd, verbose=False)
    assert output is not None
    for line in output.split("\n"):
        if line.startswith("COLMAP"):
            return float(line.split(" ")[1])
    CONSOLE.print(f"[bold red]Could not find COLMAP version. Using default {default_version}")
    return default_version


def get_vocab_tree() -> Path:
    """Return path to vocab tree. Downloads vocab tree if it doesn't exist.

    Returns:
        The path to the vocab tree.
    """
    vocab_tree_filename = Path(appdirs.user_data_dir("nerfstudio")) / "vocab_tree.fbow"

    if not vocab_tree_filename.exists():
        r = requests.get("https://demuc.de/colmap/vocab_tree_flickr100K_words32K.bin", stream=True)
        vocab_tree_filename.parent.mkdir(parents=True, exist_ok=True)
        with open(vocab_tree_filename, "wb") as f:
            total_length = r.headers.get("content-length")
            assert total_length is not None
            for chunk in track(
                r.iter_content(chunk_size=1024),
                total=int(total_length) / 1024 + 1,
                description="Downloading vocab tree...",
            ):
                if chunk:
                    f.write(chunk)
                    f.flush()
    return vocab_tree_filename


def run_command(cmd: str, verbose=False) -> Optional[str]:
    """Runs a command and returns the output.

    Args:
        cmd: Command to run.
        verbose: If True, logs the output of the command.
    Returns:
        The output of the command if return_output is True, otherwise None.
    """
    out = subprocess.run(cmd, capture_output=not verbose, shell=True, check=False)
    if out.returncode != 0:
        CONSOLE.rule("[bold red] :skull: :skull: :skull: ERROR :skull: :skull: :skull: ", style="red")
        CONSOLE.print(f"[bold red]Error running command: {cmd}")
        CONSOLE.rule(style="red")
        CONSOLE.print(out.stderr.decode("utf-8"))
        sys.exit(1)
    if out.stdout is not None:
        return out.stdout.decode("utf-8")
    return out


def get_num_frames_in_video(video: Path) -> int:
    """Returns the number of frames in a video.

    Args:
        video: Path to a video.

    Returns:
        The number of frames in a video.
    """
    cmd = f"ffprobe -v error -select_streams v:0 -count_packets \
            -show_entries stream=nb_read_packets -of csv=p=0 {video}"
    output = run_command(cmd)
    assert output is not None
    output = output.strip(" ,\t\n\r")
    return int(output)


def convert_video_to_images(
    video_path: Path, image_dir: Path, num_frames_target: int, verbose: bool = False
) -> Tuple[List[str], int]:
    """Converts a video into a sequence of images.

    Args:
        video_path: Path to the video.
        output_dir: Path to the output directory.
        num_frames_target: Number of frames to extract.
        verbose: If True, logs the output of the command.
    Returns:
        A tuple containing summary of the conversion and the number of extracted frames.
    """

    with status(msg="Converting video to images...", spinner="bouncingBall", verbose=verbose):
        # delete existing images in folder
        for img in image_dir.glob("*.png"):
            if verbose:
                CONSOLE.log(f"Deleting {img}")
            img.unlink()

        num_frames = get_num_frames_in_video(video_path)
        if num_frames == 0:
            CONSOLE.print(f"[bold red]Error: Video has no frames: {video_path}")
            sys.exit(1)
        print("Number of frames in video:", num_frames)

        out_filename = image_dir / "frame_%05d.png"
        ffmpeg_cmd = f"ffmpeg -i {video_path}"
        spacing = num_frames // num_frames_target

        if spacing > 1:
            ffmpeg_cmd += f" -vf thumbnail={spacing},setpts=N/TB -r 1"
        else:
            CONSOLE.print("[bold red]Can't satify requested number of frames. Extracting all frames.")

        ffmpeg_cmd += f" {out_filename}"

        run_command(ffmpeg_cmd, verbose=verbose)

    num_final_frames = len(list(image_dir.glob("*.png")))
    summary_log = []
    summary_log.append(f"Starting with {num_frames} video frames")
    summary_log.append(f"We extracted {num_final_frames} images")
    CONSOLE.log("[bold green]:tada: Done converting video to images.")

    return summary_log, num_final_frames


def convert_insta360_to_images(
    video_front: Path,
    video_back: Path,
    image_dir: Path,
    num_frames_target: int,
    crop_percentage: float = 0.7,
    verbose: bool = False,
) -> Tuple[List[str], int]:
    """Converts a video into a sequence of images.

    Args:
        video_front: Path to the front video.
        video_back: Path to the back video.
        output_dir: Path to the output directory.
        num_frames_target: Number of frames to extract.
        verbose: If True, logs the output of the command.
    Returns:
        A tuple containing summary of the conversion and the number of extracted frames.
    """

    with status(msg="Converting video to images...", spinner="bouncingBall", verbose=verbose):
        # delete existing images in folder
        for img in image_dir.glob("*.png"):
            if verbose:
                CONSOLE.log(f"Deleting {img}")
            img.unlink()

        num_frames_front = get_num_frames_in_video(video_front)
        num_frames_back = get_num_frames_in_video(video_back)
        if num_frames_front == 0:
            CONSOLE.print(f"[bold red]Error: Video has no frames: {video_front}")
            sys.exit(1)
        if num_frames_back == 0:
            CONSOLE.print(f"[bold red]Error: Video has no frames: {video_front}")
            sys.exit(1)

        spacing = num_frames_front // (num_frames_target // 2)
        vf_cmds = []
        if spacing > 1:
            vf_cmds = [f"thumbnail={spacing}", "setpts=N/TB"]
        else:
            CONSOLE.print("[bold red]Can't satify requested number of frames. Extracting all frames.")

        # vf_cmds.append(f"crop=iw*({crop_percentage}):ih*({crop_percentage})")
        vf_cmds.append("v360=dfisheye:equirect:ih_fov=190:iv_fov=190:yaw=-90")

        front_vf_cmds = vf_cmds + ["transpose=2"]
        back_vf_cmds = vf_cmds + ["transpose=1"]

        front_ffmpeg_cmd = f"ffmpeg -i {video_front} -vf {','.join(front_vf_cmds)} -r 1 {image_dir / 'frame_%05d.png'}"
        back_ffmpeg_cmd = (
            f"ffmpeg -i {video_back} -vf {','.join(back_vf_cmds)} -r 1 {image_dir / 'back_frame_%05d.png'}"
        )

        run_command(front_ffmpeg_cmd, verbose=verbose)
        run_command(back_ffmpeg_cmd, verbose=verbose)

        num_extracted_front_frames = len(list(image_dir.glob("frame*.png")))
        for i, img in enumerate(image_dir.glob("back_frame_*.png")):
            img.rename(image_dir / f"frame_{i+1+num_extracted_front_frames:05d}.png")

    num_final_frames = len(list(image_dir.glob("*.png")))
    summary_log = []
    summary_log.append(f"Starting with {num_frames_front + num_frames_back} video frames")
    summary_log.append(f"We extracted {num_final_frames} images")
    CONSOLE.log("[bold green]:tada: Done converting insta360 to images.")

    return summary_log, num_final_frames


def copy_images_list(
    image_paths: List[Path],
    image_dir: Path,
    crop_border_pixels: Optional[int] = None,
    verbose: bool = False,
    rename: bool = True,
) -> List[Path]:
    """Copy all images in a list of Paths. Useful for filtering from a directory.
    Args:
        image_paths: List of Paths of images to copy to a new directory.
        image_dir: Path to the output directory.
        crop_border_pixels: If not None, crops each edge by the specified number of pixels.
        verbose: If True, print extra logging.
    Returns:
        A list of the copied image Paths.
    """

    # Remove original directory only if we provide a proper image folder path
    if image_dir.is_dir() and len(image_paths):
        shutil.rmtree(image_dir, ignore_errors=True)
        image_dir.mkdir(exist_ok=True, parents=True)

    copied_image_paths = []

    # Images should be 1-indexed for the rest of the pipeline.
    for idx, image_path in enumerate(image_paths):
        if verbose:
            CONSOLE.log(f"Copying image {idx + 1} of {len(image_paths)}...")
        copied_image_path = image_dir / f"frame_{idx + 1:05d}{image_path.suffix}"
        shutil.copy(image_path, copied_image_path)
        copied_image_paths.append(copied_image_path)

    if crop_border_pixels is not None:
        file_type = image_paths[0].suffix
        filename = f"frame_%05d{file_type}"
        crop = f"crop=iw-{crop_border_pixels*2}:ih-{crop_border_pixels*2}"
        ffmpeg_cmd = f"ffmpeg -y -i {image_dir / filename} -q:v 2 -vf {crop} {image_dir / filename}"
        run_command(ffmpeg_cmd, verbose=verbose)

    num_frames = len(image_paths)

    if num_frames == 0:
        CONSOLE.log("[bold red]:skull: No usable images in the data folder.")
    else:
        CONSOLE.log("[bold green]:tada: Done copying images.")

    return copied_image_paths


def copy_images(data: Path, image_dir: Path, verbose: bool, rename: bool = True) -> int:
    """Copy images from a directory to a new directory.

    Args:
        data: Path to the directory of images.
        image_dir: Path to the output directory.
        verbose: If True, print extra logging.
    Returns:
        The number of images copied.
    """
    with status(msg="[bold yellow]Copying images...", spinner="bouncingBall", verbose=verbose):
        allowed_exts = [".jpg", ".jpeg", ".png", ".tif", ".tiff"]
        image_paths = sorted([p for p in data.glob("[!.]*") if p.suffix.lower() in allowed_exts])

        num_frames = len(copy_images_list(image_paths, image_dir, verbose))

    return num_frames


def downscale_images(image_dir: Path, num_downscales: int, verbose: bool = False) -> str:
    """Downscales the images in the directory. Uses FFMPEG.

    Assumes images are named frame_00001.png, frame_00002.png, etc.

    Args:
        image_dir: Path to the directory containing the images.
        num_downscales: Number of times to downscale the images. Downscales by 2 each time.
        verbose: If True, logs the output of the command.

    Returns:
        Summary of downscaling.
    """

    if num_downscales == 0:
        return "No downscaling performed."

    with status(msg="[bold yellow]Downscaling images...", spinner="growVertical", verbose=verbose):
        downscale_factors = [2**i for i in range(num_downscales + 1)[1:]]
        for downscale_factor in downscale_factors:
            assert downscale_factor > 1
            assert isinstance(downscale_factor, int)
            downscale_dir = image_dir.parent / f"images_{downscale_factor}"
            downscale_dir.mkdir(parents=True, exist_ok=True)
            file_type = image_dir.glob("frame_*").__next__().suffix
            filename = f"frame_%05d{file_type}"
            ffmpeg_cmd = [
                f"ffmpeg -i {image_dir / filename} ",
                f"-q:v 2 -vf scale=iw/{downscale_factor}:ih/{downscale_factor} ",
                f"{downscale_dir / filename}",
            ]
            ffmpeg_cmd = " ".join(ffmpeg_cmd)
            run_command(ffmpeg_cmd, verbose=verbose)

    CONSOLE.log("[bold green]:tada: Done downscaling images.")
    downscale_text = [f"[bold blue]{2**(i+1)}x[/bold blue]" for i in range(num_downscales)]
    downscale_text = ", ".join(downscale_text[:-1]) + " and " + downscale_text[-1]
    return f"We downsampled the images by {downscale_text}"


def run_colmap(
    image_dir: Path,
    colmap_dir: Path,
    camera_model: CameraModel,
    gpu: bool = True,
    verbose: bool = False,
    matching_method: Literal["vocab_tree", "exhaustive", "sequential"] = "vocab_tree",
    colmap_cmd: str = "colmap",
) -> None:
    """Runs COLMAP on the images.

    Args:
        image_dir: Path to the directory containing the images.
        colmap_dir: Path to the output directory.
        camera_model: Camera model to use.
        gpu: If True, use GPU.
        verbose: If True, logs the output of the command.
    """

    colmap_version = get_colmap_version(colmap_cmd)

    colmap_database_path = colmap_dir / "database.db"
    if colmap_database_path.exists():
        # Can't use missing_ok argument because of Python 3.7 compatibility.
        colmap_database_path.unlink()

    # Feature extraction
    feature_extractor_cmd = [
        f"{colmap_cmd} feature_extractor",
        f"--database_path {colmap_dir / 'database.db'}",
        f"--image_path {image_dir}",
        "--ImageReader.single_camera 1",
        f"--ImageReader.camera_model {camera_model.value}",
        f"--SiftExtraction.use_gpu {int(gpu)}",
    ]
    feature_extractor_cmd = " ".join(feature_extractor_cmd)
    with status(msg="[bold yellow]Running COLMAP feature extractor...", spinner="moon", verbose=verbose):
        run_command(feature_extractor_cmd, verbose=verbose)

    CONSOLE.log("[bold green]:tada: Done extracting COLMAP features.")

    # Feature matching
    feature_matcher_cmd = [
        f"{colmap_cmd} {matching_method}_matcher",
        f"--database_path {colmap_dir / 'database.db'}",
        f"--SiftMatching.use_gpu {int(gpu)}",
    ]
    if matching_method == "vocab_tree":
        vocab_tree_filename = get_vocab_tree()
        feature_matcher_cmd.append(f"--VocabTreeMatching.vocab_tree_path {vocab_tree_filename}")
    feature_matcher_cmd = " ".join(feature_matcher_cmd)
    with status(msg="[bold yellow]Running COLMAP feature matcher...", spinner="runner", verbose=verbose):
        run_command(feature_matcher_cmd, verbose=verbose)
    CONSOLE.log("[bold green]:tada: Done matching COLMAP features.")

    # Bundle adjustment
    sparse_dir = colmap_dir / "sparse"
    sparse_dir.mkdir(parents=True, exist_ok=True)
    mapper_cmd = [
        f"{colmap_cmd} mapper",
        f"--database_path {colmap_dir / 'database.db'}",
        f"--image_path {image_dir}",
        f"--output_path {sparse_dir}",
    ]
    if colmap_version >= 3.7:
        mapper_cmd.append("--Mapper.ba_global_function_tolerance 1e-6")

    mapper_cmd = " ".join(mapper_cmd)

    with status(
        msg="[bold yellow]Running COLMAP bundle adjustment... (This may take a while)",
        spinner="circle",
        verbose=verbose,
    ):
        run_command(mapper_cmd, verbose=verbose)
    CONSOLE.log("[bold green]:tada: Done COLMAP bundle adjustment.")
    with status(msg="[bold yellow]Refine intrinsics...", spinner="dqpb", verbose=verbose):
        bundle_adjuster_cmd = [
            f"{colmap_cmd} bundle_adjuster",
            f"--input_path {sparse_dir}/0",
            f"--output_path {sparse_dir}/0",
            "--BundleAdjustment.refine_principal_point 1",
        ]
        run_command(" ".join(bundle_adjuster_cmd), verbose=verbose)
    CONSOLE.log("[bold green]:tada: Done refining intrinsics.")


def colmap_to_json(cameras_path: Path, images_path: Path, output_dir: Path, camera_model: CameraModel) -> int:
    """Converts COLMAP's cameras.bin and images.bin to a JSON file.

    Args:
        cameras_path: Path to the cameras.bin file.
        images_path: Path to the images.bin file.
        output_dir: Path to the output directory.
        camera_model: Camera model used.

    Returns:
        The number of registered images.
    """

    cameras = colmap_utils.read_cameras_binary(cameras_path)
    images = colmap_utils.read_images_binary(images_path)

    # Only supports one camera
    camera_params = cameras[1].params

    frames = []
    for _, im_data in images.items():
        rotation = colmap_utils.qvec2rotmat(im_data.qvec)
        translation = im_data.tvec.reshape(3, 1)
        w2c = np.concatenate([rotation, translation], 1)
        w2c = np.concatenate([w2c, np.array([[0, 0, 0, 1]])], 0)
        c2w = np.linalg.inv(w2c)
        # Convert from COLMAP's camera coordinate system to ours
        c2w[0:3, 1:3] *= -1
        c2w = c2w[np.array([1, 0, 2, 3]), :]
        c2w[2, :] *= -1

        name = Path(f"./images/{im_data.name}")

        frame = {
            "file_path": name.as_posix(),
            "transform_matrix": c2w.tolist(),
        }
        frames.append(frame)

    out = {
        "fl_x": float(camera_params[0]),
        "fl_y": float(camera_params[1]),
        "cx": float(camera_params[2]),
        "cy": float(camera_params[3]),
        "w": cameras[1].width,
        "h": cameras[1].height,
        "camera_model": camera_model.value,
    }

    if camera_model == CameraModel.OPENCV:
        out.update(
            {
                "k1": float(camera_params[4]),
                "k2": float(camera_params[5]),
                "p1": float(camera_params[6]),
                "p2": float(camera_params[7]),
            }
        )
    if camera_model == CameraModel.OPENCV_FISHEYE:
        out.update(
            {
                "k1": float(camera_params[4]),
                "k2": float(camera_params[5]),
                "k3": float(camera_params[6]),
                "k4": float(camera_params[7]),
            }
        )

    out["frames"] = frames

    with open(output_dir / "transforms.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=4)

    return len(frames)


def run_opensfm(
    image_dir: Path, opensfm_dir: Path, camera_model: CameraModel, opensfm_install: Path, verbose=False
) -> None:
    exe = opensfm_install / "bin" / "opensfm"
    # TODO cleanup config
    config = {
        "processes": 12,
        "matching_order_neighbors": 20,
        "feature_process_size": 1024,
        "triangulation_threshold": 0.006,
        "triangulation_type": "ROBUST",
        "min_track_length": 2,
        "retriangulation_ratio": 1.5,
        "bundle_new_points_ratio": 1.5,
    }
    with open(opensfm_dir / "config.yaml", "w") as config_file:
        yaml.dump(config, config_file)
    example_img = image_dir.glob("*").__next__()
    pil_im = Image.open(example_img)
    if camera_model == CameraModel.OPENCV:
        proj_type = "brown"
    elif camera_model == CameraModel.OPENCV_FISHEYE:
        proj_type = "fisheye_opencv"
    elif camera_model == CameraModel.EQUIRECTANGULAR:
        proj_type = "equirectangular"
    cam_overrides = {
        "all": {
            "projection_type": proj_type,
            "width": pil_im.width,
            "height": pil_im.height,
            "focal_x": 0.85,
            "focal_y": 0.85,
        }
    }
    with open(opensfm_dir / "camera_models_overrides.json", "w") as cam_file:
        json.dump(cam_overrides, cam_file)

    run_command(f"cp -r {image_dir} {opensfm_dir/'images'}")
    metadata_cmd = [f"{exe} extract_metadata", f"{opensfm_dir}"]
    metadata_cmd = " ".join(metadata_cmd)
    with status(msg="[bold yellow]Extracting OpenSfM metadata...", spinner="moon", verbose=verbose):
        run_command(metadata_cmd, verbose=verbose)
    CONSOLE.log("[bold green]:tada: Done OpenSfM metadata.")

    feature_extractor_cmd = [f"{exe} detect_features", f"{opensfm_dir}"]
    feature_extractor_cmd = " ".join(feature_extractor_cmd)
    with status(msg="[bold yellow]Running OpenSfM feature extractor...", spinner="runner", verbose=verbose):
        run_command(feature_extractor_cmd, verbose=verbose)
    CONSOLE.log("[bold green]:tada: Done extracting image features.")

    matcher_cmd = [f"{exe} match_features", f"{opensfm_dir}"]
    matcher_cmd = " ".join(matcher_cmd)
    with status(msg="[bold yellow]Running OpenSfM feature matcher...", spinner="runner", verbose=verbose):
        run_command(matcher_cmd, verbose=verbose)
    CONSOLE.log("[bold green]:tada: Done matching image features.")

    tracks_cmd = [f"{exe} create_tracks", f"{opensfm_dir}"]
    tracks_cmd = " ".join(tracks_cmd)
    with status(msg="[bold yellow]Merging OpenSfM features into tracked points...", spinner="dqpb", verbose=verbose):
        run_command(tracks_cmd, verbose=verbose)
    CONSOLE.log("[bold green]:tada: Done merging features.")

    recon_cmd = [f"{exe} reconstruct", f"{opensfm_dir}"]
    recon_cmd = " ".join(recon_cmd)
    with status(
        msg="[bold yellow]Running OpenSfM bundle adjustment... (this can take 10-15 minutes)",
        spinner="dqpb",
        verbose=verbose,
    ):
        run_command(recon_cmd, verbose=verbose)
    CONSOLE.log("[bold green]:tada: Done running bundle adjustment.")


def opensfm_to_json(reconstruction_path: Path, output_dir: Path, camera_model: CameraModel) -> int:
    sfm_out = (json.load(open(reconstruction_path, "r")))[0]
    cams = sfm_out["cameras"]
    shots = sfm_out["shots"]
    frames = []
    for shot_name, shot_info in shots.items():
        # enumerates through images in the capture
        aa_vec = np.array(shot_info["rotation"])  # 3D axis angle repr
        angle = np.linalg.norm(aa_vec)
        if angle > 1e-8:
            # normalize the axis-angle repr if angle is large enough
            aa_vec /= angle
            qx = aa_vec[0] * np.sin(angle / 2)
            qy = aa_vec[1] * np.sin(angle / 2)
            qz = aa_vec[2] * np.sin(angle / 2)
            qw = np.cos(angle / 2)
        else:
            qx, qy, qz, qw = 0.0, 0.0, 0.0, 1.0
        rotation = colmap_utils.qvec2rotmat(np.array([qw, qx, qy, qz]))
        translation = np.array(shot_info["translation"]).reshape(3, 1)
        w2c = np.concatenate([rotation, translation], 1)
        w2c = np.concatenate([w2c, np.array([[0, 0, 0, 1]])], 0)
        c2w = np.linalg.inv(w2c)
        # Convert camera +z forwards (OpenSfM) convention to -z forwards (NeRF) convention
        # Equivalent to right-multiplication by diag([1, -1, -1, 1])
        c2w[0:3, 1:3] *= -1
        # Rotation around global z-axis by -90 degrees
        c2w = c2w[np.array([1, 0, 2, 3]), :]
        c2w[2, :] *= -1

        name = Path(f"./images/{shot_name}")

        frame = {
            "file_path": str(name),
            "transform_matrix": c2w.tolist(),
        }
        frames.append(frame)
    # For now just assume it's all the same camera
    cam = cams[list(cams.keys())[0]]
    out = {
        "fl_x": float(cam["focal_x"]) if "focal_x" in cam else float(cam["height"]),
        "fl_y": float(cam["focal_y"]) if "focal_y" in cam else float(cam["height"]),
        "cx": float(cam["c_x"]) if "c_x" in cam else 0.5 * cam["width"],
        "cy": float(cam["c_y"]) if "c_y" in cam else 0.5 * cam["height"],
        "w": int(cam["width"]),
        "h": int(cam["height"]),
        "camera_model": camera_model.value,
    }
    if camera_model == CameraModel.OPENCV:
        out.update(
            {
                "k1": float(cam["k1"]),
                "k2": float(cam["k2"]),
                "p1": float(cam["p1"]),
                "p2": float(cam["p2"]),
            }
        )
    if camera_model == CameraModel.OPENCV_FISHEYE:
        # TODO: find the opensfm camera model that uses these params
        out.update(
            {
                "k1": float(cam["k1"]),
                "k2": float(cam["k2"]),
                "k3": float(cam["k3"]),
                "k4": float(cam["k4"]),
            }
        )

    out["frames"] = frames

    with open(output_dir / "transforms.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=4)

    return len(frames)


def record3d_to_json(images_paths: List[Path], metadata_path: Path, output_dir: Path, indices: np.ndarray) -> int:
    """Converts Record3D's metadata and image paths to a JSON file.

    Args:
        images_paths: list if image paths.
        metadata_path: Path to the Record3D metadata JSON file.
        output_dir: Path to the output directory.
        indices: Indices to sample the metadata_path. Should be the same length as images_paths.

    Returns:
        The number of registered images.
    """

    assert len(images_paths) == len(indices)

    metadata_dict = io.load_from_json(metadata_path)

    poses_data = np.array(metadata_dict["poses"])  # (N, 3, 4)
    camera_to_worlds = np.concatenate(
        [Rotation.from_quat(poses_data[:, :4]).as_matrix(), poses_data[:, 4:, None]],
        axis=-1,
    ).astype(np.float32)
    camera_to_worlds = camera_to_worlds[indices]

    homogeneous_coord = np.zeros_like(camera_to_worlds[..., :1, :])
    homogeneous_coord[..., :, 3] = 1
    camera_to_worlds = np.concatenate([camera_to_worlds, homogeneous_coord], -2)

    frames = []
    for i, im_path in enumerate(images_paths):
        c2w = camera_to_worlds[i]
        frame = {
            "file_path": im_path.as_posix(),
            "transform_matrix": c2w.tolist(),
        }
        frames.append(frame)

    # Camera intrinsics
    K = np.array(metadata_dict["K"]).reshape((3, 3)).T
    focal_length = K[0, 0]

    H = metadata_dict["h"]
    W = metadata_dict["w"]

    # TODO(akristoffersen): The metadata dict comes with principle points,
    # but caused errors in image coord indexing. Should update once that is fixed.
    cx, cy = W / 2, H / 2

    out = {
        "fl_x": focal_length,
        "fl_y": focal_length,
        "cx": cx,
        "cy": cy,
        "w": W,
        "h": H,
        "camera_model": CAMERA_MODELS["perspective"].name,
    }

    out["frames"] = frames

    with open(output_dir / "transforms.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=4)

    return len(frames)


def get_insta360_filenames(data: Path) -> Tuple[Path, Path]:
    """Returns the filenames of the Insta360 videos from a single video file.

    Example input name: VID_20220212_070353_00_003.insv

    Args:
        data: Path to a Insta360 file.

    Returns:
        The filenames of the Insta360 videios.
    """
    if data.suffix != ".insv":
        raise ValueError("The input file must be an .insv file.")
    file_parts = data.stem.split("_")

    stem_back = f"VID_{file_parts[1]}_{file_parts[2]}_00_{file_parts[4]}.insv"
    stem_front = f"VID_{file_parts[1]}_{file_parts[2]}_10_{file_parts[4]}.insv"

    filename_back = data.parent / stem_back
    filename_front = data.parent / stem_front

    if not filename_back.exists():
        raise FileNotFoundError(f"Could not find {filename_back}")
    if not filename_front.exists():
        raise FileNotFoundError(f"Could not find {filename_front}")

    return filename_back, filename_front


def get_matching_summary(num_intial_frames: int, num_matched_frames: int) -> str:
    """Returns a summary of the matching results.

    Args:
        num_intial_frames: The number of initial frames.
        num_matched_frames: The number of matched frames.

    Returns:
        A summary of the matching results.
    """
    match_ratio = num_matched_frames / num_intial_frames
    if match_ratio == 1:
        return "[bold green]COLAMP found poses for all images, CONGRATS!"
    if match_ratio < 0.4:
        result = f"[bold red]COLMAP only found poses for {num_matched_frames / num_intial_frames * 100:.2f}%"
        result += " of the images. This is low.\nThis can be caused by a variety of reasons,"
        result += " such poor scene coverage, blurry images, or large exposure changes."
        return result
    if match_ratio < 0.8:
        result = f"[bold yellow]COLMAP only found poses for {num_matched_frames / num_intial_frames * 100:.2f}%"
        result += " of the images.\nThis isn't great, but may be ok."
        result += "\nMissing poses can be caused by a variety of reasons, such poor scene coverage, blurry images,"
        result += " or large exposure changes."
        return result
    return f"[bold green]COLMAP found poses for {num_matched_frames / num_intial_frames * 100:.2f}% of the images."


def polycam_to_json(
    image_filenames: List[Path],
    cameras_dir: Path,
    output_dir: Path,
    min_blur_score: float = 0.0,
    crop_border_pixels: int = 0,
) -> List[str]:
    """Convert Polycam data into a nerfstudio dataset.

    Args:
        image_filenames: List of paths to the original images.
        cameras_dir: Path to the polycam cameras directory.
        output_dir: Path to the output directory.
        min_blur_score: Minimum blur score to use an image. Images below this value will be skipped.
        crop_border_pixels: Number of pixels to crop from each border of the image.

    Returns:
        Summary of the conversion.
    """

    frame_json = io.load_from_json(cameras_dir / f"{image_filenames[0].stem}.json")

    # TODO Add per-frame intrinsics

    data = {}
    data["fl_x"] = frame_json["fx"]
    data["fl_y"] = frame_json["fy"]
    data["cx"] = frame_json["cx"] - crop_border_pixels
    data["cy"] = frame_json["cy"] - crop_border_pixels
    data["w"] = frame_json["width"] - crop_border_pixels * 2
    data["h"] = frame_json["height"] - crop_border_pixels * 2
    data["camera_model"] = CAMERA_MODELS["perspective"].value
    # Needs to be a string for camera_utils.auto_orient_and_center_poses
    data["orientation_override"] = "none"

    frames = []
    skipped_frames = 0
    for i, image_filename in enumerate(image_filenames):
        json_filename = cameras_dir / f"{image_filename.stem}.json"
        frame_json = io.load_from_json(json_filename)
        if "blur_score" in frame_json and frame_json["blur_score"] < min_blur_score:
            skipped_frames += 1
            continue
        frame = {}
        frame["file_path"] = f"./images/frame_{i+1:05d}{image_filename.suffix}"
        # Transform matrix to nerfstudio format. Please refer to the documentation for coordinate system conventions.
        frame["transform_matrix"] = [
            [frame_json["t_20"], frame_json["t_21"], frame_json["t_22"], frame_json["t_23"]],
            [frame_json["t_00"], frame_json["t_01"], frame_json["t_02"], frame_json["t_03"]],
            [frame_json["t_10"], frame_json["t_11"], frame_json["t_12"], frame_json["t_13"]],
            [0.0, 0.0, 0.0, 1.0],
        ]
        frames.append(frame)
    data["frames"] = frames

    with open(output_dir / "transforms.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

    summary = []
    if skipped_frames > 0:
        summary.append(f"Skipped {skipped_frames} frames due to low blur score.")
    summary.append(f"Final dataset is {len(image_filenames) - skipped_frames} frames.")

    if len(image_filenames) - skipped_frames == 0:
        CONSOLE.print("[bold red]No images remain after filtering, exiting")
        sys.exit(1)

    return summary


@dataclass
class ProcessImages:
    """Process images into a nerfstudio dataset.

    This script does the following:

    1. Scales images to a specified size.
    2. Calculates the camera poses for each image using `COLMAP <https://colmap.github.io/>`_.
    """

    data: Path
    """Path the data, either a video file or a directory of images."""
    output_dir: Path
    """Path to the output directory."""
    camera_type: Literal["perspective", "fisheye", "equirectangular"] = "perspective"
    """Camera model to use."""
    sfm_method: Literal["colmap", "OpenSfM"] = "OpenSfM"
    """Which sfm solver to use"""
    opensfm_dir: Optional[Path] = None
    """If sfm_method is OpenSfM, specify the executable to use here"""
    matching_method: Literal["exhaustive", "sequential", "vocab_tree"] = "vocab_tree"
    """Feature matching method to use. Vocab tree is recommended for a balance of speed and
        accuracy. Exhaustive is slower but more accurate. Sequential is faster but should only be used for videos."""
    num_downscales: int = 3
    """Number of times to downscale the images. Downscales by 2 each time. For example a value of 3
        will downscale the images by 2x, 4x, and 8x."""
    skip_colmap: bool = False
    """If True, skips COLMAP and generates transforms.json if possible."""
    colmap_cmd: str = "colmap"
    """How to call the COLMAP executable."""
    gpu: bool = True
    """If True, use GPU."""
    verbose: bool = False
    """If True, print extra logging."""

    def main(self) -> None:
        """Process images into a nerfstudio dataset."""
        install_checks.check_ffmpeg_installed()
        install_checks.check_colmap_installed()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        image_dir = self.output_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        summary_log = []

        # Copy images to output directory
        rename_images = self.sfm_method != "OpenSfM"  # OpenSfM does not rename images
        num_frames = copy_images(self.data, image_dir=image_dir, verbose=self.verbose, rename=rename_images)
        summary_log.append(f"Starting with {num_frames} images")

        # Downscale images
        summary_log.append(downscale_images(image_dir, self.num_downscales, verbose=self.verbose))

        if self.sfm_method == "colmap":
            # Run COLMAP
            colmap_dir = self.output_dir / "colmap"
            if not self.skip_colmap:
                colmap_dir.mkdir(parents=True, exist_ok=True)

                run_colmap(
                    image_dir=image_dir,
                    colmap_dir=colmap_dir,
                    camera_model=CAMERA_MODELS[self.camera_type],
                    gpu=self.gpu,
                    verbose=self.verbose,
                    matching_method=self.matching_method,
                    colmap_cmd=self.colmap_cmd,
                )

            # Save transforms.json
            if (colmap_dir / "sparse" / "0" / "cameras.bin").exists():
                with CONSOLE.status("[bold yellow]Saving results to transforms.json", spinner="balloon"):
                    num_matched_frames = colmap_to_json(
                        cameras_path=colmap_dir / "sparse" / "0" / "cameras.bin",
                        images_path=colmap_dir / "sparse" / "0" / "images.bin",
                        output_dir=self.output_dir,
                        camera_model=CAMERA_MODELS[self.camera_type],
                    )
                    summary_log.append(f"Colmap matched {num_matched_frames} images")
                summary_log.append(get_matching_summary(num_frames, num_matched_frames))
            else:
                CONSOLE.log(
                    "[bold yellow]Warning: could not find existing COLMAP results. Not generating transforms.json"
                )
        elif self.sfm_method == "OpenSfM":
            assert self.opensfm_dir is not None, "Please provide the path to the OpenSfM directory <...>/bin/opensfm"
            # Run the OpenSfM solver
            opensfm_dir = self.output_dir / "opensfm"
            opensfm_dir.mkdir(parents=True, exist_ok=True)
            run_opensfm(image_dir, opensfm_dir, CAMERA_MODELS[self.camera_type], self.opensfm_dir)
            # Save transforms.json
            if (opensfm_dir / "reconstruction.json").exists():
                with CONSOLE.status("[bold yellow]Saving results to transforms.json", spinner="balloon"):
                    opensfm_to_json(
                        opensfm_dir / "reconstruction.json", self.output_dir, CAMERA_MODELS[self.camera_type]
                    )
            else:
                CONSOLE.log(
                    "[bold yellow]Warning: could not find existing OpenSfM results. Not generating transforms.json"
                )

        CONSOLE.rule("[bold green]:tada: :tada: :tada: All DONE :tada: :tada: :tada:")

        for summary in summary_log:
            CONSOLE.print(summary, justify="center")
        CONSOLE.rule()


@dataclass
class ProcessVideo:
    """Process videos into a nerfstudio dataset.

    This script does the following:

    1. Converts the video into images.
    2. Scales images to a specified size.
    3. Calculates the camera poses for each image using `COLMAP <https://colmap.github.io/>`_.
    """

    data: Path
    """Path the data, either a video file or a directory of images."""
    output_dir: Path
    """Path to the output directory."""
    num_frames_target: int = 150
    """Target number of frames to use for the dataset, results may not be exact."""
    camera_type: Literal["perspective", "fisheye", "equirectangular"] = "perspective"
    """Camera model to use."""
    sfm_method: Literal["colmap", "OpenSfM"] = "OpenSfM"
    """Which sfm solver to use"""
    opensfm_dir: Optional[Path] = None
    """If sfm_method is OpenSfM, specify the executable to use here"""
    matching_method: Literal["exhaustive", "sequential", "vocab_tree"] = "vocab_tree"
    """Feature matching method to use. Vocab tree is recommended for a balance of speed and
        accuracy. Exhaustive is slower but more accurate. Sequential is faster but should only be used for videos."""
    num_downscales: int = 3
    """Number of times to downscale the images. Downscales by 2 each time. For example a value of 3
        will downscale the images by 2x, 4x, and 8x."""
    skip_colmap: bool = False
    """If True, skips COLMAP and generates transforms.json if possible."""
    colmap_cmd: str = "colmap"
    """How to call the COLMAP executable."""
    gpu: bool = True
    """If True, use GPU."""
    verbose: bool = False
    """If True, print extra logging."""

    def main(self) -> None:
        """Process video into a nerfstudio dataset."""
        install_checks.check_ffmpeg_installed()
        install_checks.check_colmap_installed()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        image_dir = self.output_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        # Convert video to images
        summary_log, num_extracted_frames = convert_video_to_images(
            self.data, image_dir=image_dir, num_frames_target=self.num_frames_target, verbose=self.verbose
        )

        # Downscale images
        summary_log.append(downscale_images(image_dir, self.num_downscales, verbose=self.verbose))

        if self.sfm_method == "colmap":
            # Run Colmap
            colmap_dir = self.output_dir / "colmap"
            if not self.skip_colmap:
                colmap_dir.mkdir(parents=True, exist_ok=True)

                run_colmap(
                    image_dir=image_dir,
                    colmap_dir=colmap_dir,
                    camera_model=CAMERA_MODELS[self.camera_type],
                    gpu=self.gpu,
                    verbose=self.verbose,
                    matching_method=self.matching_method,
                    colmap_cmd=self.colmap_cmd,
                )

            # Save transforms.json
            if (colmap_dir / "sparse" / "0" / "cameras.bin").exists():
                with CONSOLE.status("[bold yellow]Saving results to transforms.json", spinner="balloon"):
                    num_matched_frames = colmap_to_json(
                        cameras_path=colmap_dir / "sparse" / "0" / "cameras.bin",
                        images_path=colmap_dir / "sparse" / "0" / "images.bin",
                        output_dir=self.output_dir,
                        camera_model=CAMERA_MODELS[self.camera_type],
                    )
                    summary_log.append(f"Colmap matched {num_matched_frames} images")
                summary_log.append(get_matching_summary(num_extracted_frames, num_matched_frames))
            else:
                CONSOLE.log(
                    "[bold yellow]Warning: could not find existing COLMAP results. Not generating transforms.json"
                )
        elif self.sfm_method == "OpenSfM":
            assert self.opensfm_dir is not None, "Please provide the path to the OpenSfM directory <...>/bin/opensfm"
            # Run the OpenSfM solver
            opensfm_dir = self.output_dir / "opensfm"
            opensfm_dir.mkdir(parents=True, exist_ok=True)
            run_opensfm(image_dir, opensfm_dir, CAMERA_MODELS[self.camera_type], self.opensfm_dir)
            # Save transforms.json
            if (opensfm_dir / "reconstruction.json").exists():
                with CONSOLE.status("[bold yellow]Saving results to transforms.json", spinner="balloon"):
                    opensfm_to_json(
                        opensfm_dir / "reconstruction.json", self.output_dir, CAMERA_MODELS[self.camera_type]
                    )
            else:
                CONSOLE.log(
                    "[bold yellow]Warning: could not find existing OpenSfM results. Not generating transforms.json"
                )

        CONSOLE.rule("[bold green]:tada: :tada: :tada: All DONE :tada: :tada: :tada:")

        for summary in summary_log:
            CONSOLE.print(summary, justify="center")
        CONSOLE.rule()


@dataclass
class ProcessInsta360:
    """Process Insta360 videos into a nerfstudio dataset. Currently this uses a center crop of the raw data
    so data at the extreme edges of the video will be lost.

    Expects data from a 2 camera Insta360, single or >2 camera models will not work.
    (tested with Insta360 One X2)

    This script does the following:

    1. Converts the videos into images.
    2. Scales images to a specified size.
    3. Calculates the camera poses for each image using `COLMAP <https://colmap.github.io/>`_.
    """

    data: Path
    """Path the data, It should be one of the 3 .insv files saved with each capture (Any work)."""
    output_dir: Path
    """Path to the output directory."""
    num_frames_target: int = 400
    """Target number of frames to use for the dataset, results may not be exact."""
    matching_method: Literal["exhaustive", "sequential", "vocab_tree"] = "vocab_tree"
    """Feature matching method to use. Vocab tree is recommended for a balance of speed and
        accuracy. Exhaustive is slower but more accurate. Sequential is faster but should only be used for videos."""
    num_downscales: int = 3
    """Number of times to downscale the images. Downscales by 2 each time. For example a value of 3
        will downscale the images by 2x, 4x, and 8x."""
    skip_colmap: bool = False
    """If True, skips COLMAP and generates transforms.json if possible."""
    colmap_cmd: str = "colmap"
    """How to call the COLMAP executable."""
    gpu: bool = True
    """If True, use GPU."""
    verbose: bool = False
    """If True, print extra logging."""

    def main(self) -> None:
        """Process video into a nerfstudio dataset."""
        install_checks.check_ffmpeg_installed()
        install_checks.check_colmap_installed()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        image_dir = self.output_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        filename_back, filename_front = get_insta360_filenames(self.data)

        # Convert video to images
        summary_log, num_extracted_frames = convert_insta360_to_images(
            video_front=filename_front,
            video_back=filename_back,
            image_dir=image_dir,
            num_frames_target=self.num_frames_target,
            verbose=self.verbose,
        )

        # Downscale images
        summary_log.append(downscale_images(image_dir, self.num_downscales, verbose=self.verbose))

        # Run Colmap
        opensfm_dir = self.output_dir / "opensfm"
        if not self.skip_colmap:
            opensfm_dir.mkdir(parents=True, exist_ok=True)

            run_opensfm(
                image_dir=image_dir,
                opensfm_dir=opensfm_dir,
                camera_model=CAMERA_MODELS["equirectangular"],
                gpu=self.gpu,
                verbose=self.verbose,
                matching_method=self.matching_method,
                opensfm_install=Path("/scratch2/ksalahi/OpenSfM"),
            )

        # Save transforms.json
        if (opensfm_dir / "sparse" / "0" / "cameras.bin").exists():
            with CONSOLE.status("[bold yellow]Saving results to transforms.json", spinner="balloon"):
                num_matched_frames = opensfm_to_json(
                    cameras_path=opensfm_dir / "sparse" / "0" / "cameras.bin",
                    images_path=opensfm_dir / "sparse" / "0" / "images.bin",
                    output_dir=self.output_dir,
                    camera_model=CAMERA_MODELS["equirectangular"],
                )
                summary_log.append(f"Colmap matched {num_matched_frames} images")
            summary_log.append(get_matching_summary(num_extracted_frames, num_matched_frames))
        else:
            CONSOLE.log("[bold yellow]Warning: could not find existing COLMAP results. Not generating transforms.json")

        CONSOLE.rule("[bold green]:tada: :tada: :tada: All DONE :tada: :tada: :tada:")

        for summary in summary_log:
            CONSOLE.print(summary, justify="center")
        CONSOLE.rule()


@dataclass
class ProcessRecord3D:
    """Process Record3D data into a nerfstudio dataset.

    This script does the following:

    1. Scales images to a specified size.
    2. Converts Record3D poses into the nerfstudio format.
    """

    data: Path
    """Path to the record3D data."""
    output_dir: Path
    """Path to the output directory."""
    num_downscales: int = 3
    """Number of times to downscale the images. Downscales by 2 each time. For example a value of 3
        will downscale the images by 2x, 4x, and 8x."""
    max_dataset_size: int = 300
    """Max number of images to train on. If the dataset has more, images will be sampled approximately evenly. If -1,
    use all images."""

    verbose: bool = False
    """If True, print extra logging."""

    def main(self) -> None:
        """Process images into a nerfstudio dataset."""

        self.output_dir.mkdir(parents=True, exist_ok=True)
        image_dir = self.output_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        summary_log = []

        record3d_image_dir = self.data / "rgb"

        if not record3d_image_dir.exists():
            raise ValueError(f"Image directory {image_dir} doesn't exist")

        record3d_image_filenames = []
        for f in record3d_image_dir.iterdir():
            if f.stem.isdigit():  # removes possible duplicate images (for example, 123(3).jpg)
                record3d_image_filenames.append(f)

        record3d_image_filenames = sorted(record3d_image_filenames, key=lambda fn: int(fn.stem))
        num_images = len(record3d_image_filenames)
        idx = np.arange(num_images)
        if self.max_dataset_size != -1 and num_images > self.max_dataset_size:
            idx = np.round(np.linspace(0, num_images - 1, self.max_dataset_size)).astype(int)

        record3d_image_filenames = list(np.array(record3d_image_filenames)[idx])
        # Copy images to output directory
        copied_image_paths = copy_images_list(record3d_image_filenames, image_dir=image_dir, verbose=self.verbose)
        num_frames = len(copied_image_paths)

        copied_image_paths = [Path("images/" + copied_image_path.name) for copied_image_path in copied_image_paths]
        summary_log.append(f"Used {num_frames} images out of {num_images} total")
        if self.max_dataset_size > 0:
            summary_log.append(
                "To change the size of the dataset add the argument --max_dataset_size to larger than the "
                f"current value ({self.max_dataset_size}), or -1 to use all images."
            )

        # Downscale images
        summary_log.append(downscale_images(image_dir, self.num_downscales, verbose=self.verbose))

        metadata_path = self.data / "metadata.json"
        record3d_to_json(copied_image_paths, metadata_path, self.output_dir, indices=idx)
        CONSOLE.rule("[bold green]:tada: :tada: :tada: All DONE :tada: :tada: :tada:")

        for summary in summary_log:
            CONSOLE.print(summary, justify="center")
        CONSOLE.rule()


@dataclass
class ProcessPolycam:
    """Process Polycam data into a nerfstudio dataset.

    To capture data, use the Polycam app on an iPhone or iPad with LiDAR. The capture must be in LiDAR or ROOM mode.
    Developer mode must be enabled in the app settings, this will enable a raw data export option in the export menus.
    The exported data folder is used as the input to this script.

    This script does the following:

    1. Scales images to a specified size.
    2. Converts Polycam poses into the nerfstudio format.
    """

    data: Path
    """Path the polycam export data folder. Can be .zip file or folder."""
    output_dir: Path
    """Path to the output directory."""
    num_downscales: int = 3
    """Number of times to downscale the images. Downscales by 2 each time. For example a value of 3
        will downscale the images by 2x, 4x, and 8x."""
    use_uncorrected_images: bool = False
    """If True, use the raw images from the polycam export. If False, use the corrected images."""
    max_dataset_size: int = 600
    """Max number of images to train on. If the dataset has more, images will be sampled approximately evenly. If -1,
    use all images."""
    min_blur_score: float = 25
    """Minimum blur score to use an image. If the blur score is below this value, the image will be skipped."""
    crop_border_pixels: int = 15
    """Number of pixels to crop from each border of the image. Useful as borders may be black due to undistortion."""

    verbose: bool = False
    """If True, print extra logging."""

    def main(self) -> None:
        """Process images into a nerfstudio dataset."""

        self.output_dir.mkdir(parents=True, exist_ok=True)
        image_dir = self.output_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        summary_log = []

        if self.data.suffix == ".zip":
            with zipfile.ZipFile(self.data, "r") as zip_ref:
                zip_ref.extractall(self.output_dir)
                extracted_folder = zip_ref.namelist()[0].split("/")[0]
            self.data = self.output_dir / extracted_folder

        if (self.data / "keyframes" / "corrected_images").exists() and not self.use_uncorrected_images:
            polycam_image_dir = self.data / "keyframes" / "corrected_images"
            polycam_cameras_dir = self.data / "keyframes" / "corrected_cameras"
        else:
            polycam_image_dir = self.data / "keyframes" / "images"
            polycam_cameras_dir = self.data / "keyframes" / "cameras"
            self.crop_border_pixels = 0
            if not self.use_uncorrected_images:
                CONSOLE.print("[bold yellow]Corrected images not found, using raw images.")

        if not polycam_image_dir.exists():
            raise ValueError(f"Image directory {polycam_image_dir} doesn't exist")

        # Copy images to output directory

        polycam_image_filenames = []
        for f in polycam_image_dir.iterdir():
            polycam_image_filenames.append(f)
        polycam_image_filenames = sorted(polycam_image_filenames, key=lambda fn: int(fn.stem))
        num_images = len(polycam_image_filenames)
        idx = np.arange(num_images)
        if self.max_dataset_size != -1 and num_images > self.max_dataset_size:
            idx = np.round(np.linspace(0, num_images - 1, self.max_dataset_size)).astype(int)

        polycam_image_filenames = list(np.array(polycam_image_filenames)[idx])
        # Copy images to output directory
        copied_image_paths = copy_images_list(
            polycam_image_filenames,
            image_dir=image_dir,
            crop_border_pixels=self.crop_border_pixels,
            verbose=self.verbose,
        )
        num_frames = len(copied_image_paths)

        copied_image_paths = [Path("images/" + copied_image_path.name) for copied_image_path in copied_image_paths]

        if self.max_dataset_size > 0 and num_frames != num_images:
            summary_log.append(f"Started with {num_frames} images out of {num_images} total")
            summary_log.append(
                "To change the size of the dataset add the argument --max_dataset_size to larger than the "
                f"current value ({self.max_dataset_size}), or -1 to use all images."
            )
        else:
            summary_log.append(f"Started with {num_frames} images")

        # Downscale images
        summary_log.append(downscale_images(image_dir, self.num_downscales, verbose=self.verbose))

        # Save json
        if num_frames == 0:
            CONSOLE.print("[bold red]No images found, exiting")
            sys.exit(1)
        summary_log.extend(
            polycam_to_json(
                image_filenames=polycam_image_filenames,
                cameras_dir=polycam_cameras_dir,
                output_dir=self.output_dir,
                min_blur_score=self.min_blur_score,
                crop_border_pixels=self.crop_border_pixels,
            )
        )

        CONSOLE.rule("[bold green]:tada: :tada: :tada: All DONE :tada: :tada: :tada:")

        for summary in summary_log:
            CONSOLE.print(summary, justify="center")
        CONSOLE.rule()


Commands = Union[
    Annotated[ProcessImages, tyro.conf.subcommand(name="images")],
    Annotated[ProcessVideo, tyro.conf.subcommand(name="video")],
    Annotated[ProcessPolycam, tyro.conf.subcommand(name="polycam")],
    Annotated[ProcessInsta360, tyro.conf.subcommand(name="insta360")],
    Annotated[ProcessRecord3D, tyro.conf.subcommand(name="record3d")],
]


def entrypoint():
    """Entrypoint for use with pyproject scripts."""
    tyro.extras.set_accent_color("bright_yellow")
    tyro.cli(Commands).main()


if __name__ == "__main__":
    entrypoint()

# For sphinx docs
get_parser_fn = lambda: tyro.extras.get_parser(Commands)  # type: ignore
