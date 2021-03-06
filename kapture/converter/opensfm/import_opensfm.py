import logging
import os
import os.path as path
import numpy as np
import quaternion
import gzip
import pickle
import json
from tqdm import tqdm
from typing import Optional, Dict
# kapture
import kapture
from kapture.io.structure import delete_existing_kapture_files
from kapture.io.csv import kapture_to_dir
from kapture.io.features import get_keypoints_fullpath, image_keypoints_from_file
from kapture.io.features import get_descriptors_fullpath, image_descriptors_from_file
from kapture.io.features import get_matches_fullpath, image_matches_from_file
from kapture.io.records import TransferAction, transfer_files_from_dir, get_record_fullpath
import kapture.io.csv as csv
import kapture.io.features
from kapture.io.records import TransferAction, import_record_data_from_dir_auto
from kapture.io.structure import delete_existing_kapture_files

logger = logging.getLogger('opensfm')

"""
opensfm_project/
├── config.yaml
├── images/
├── masks/
├── gcp_list.txt
├── exif/
├── camera_models.json
├── features/
├── matches/
├── tracks.csv
├── reconstruction.json
├── reconstruction.meshed.json
└── undistorted/
    ├── images/
    ├── masks/
    ├── tracks.csv
    ├── reconstruction.json
    └── depthmaps/
        └── merged.ply
"""

"""
reconstruction.json: [RECONSTRUCTION, ...]

RECONSTRUCTION: {
    "cameras": {
        CAMERA_ID: CAMERA,
        ...
    },
    "shots": {
        SHOT_ID: SHOT,
        ...
    },
    "points": {
        POINT_ID: POINT,
        ...
    }
}

CAMERA: {
    "projection_type": "perspective",  # Can be perspective, brown, fisheye or equirectangular
    "width": NUMBER,                   # Image width in pixels
    "height": NUMBER,                  # Image height in pixels

    # Depending on the projection type more parameters are stored.
    # These are the parameters of the perspective camera.
    "focal": NUMBER,                   # Estimated focal length
    "k1": NUMBER,                      # Estimated distortion coefficient
    "k2": NUMBER,                      # Estimated distortion coefficient
}

SHOT: {
    "camera": CAMERA_ID,
    "rotation": [X, Y, Z],      # Estimated rotation as an angle-axis vector
    "translation": [X, Y, Z],   # Estimated translation
    "gps_position": [X, Y, Z],  # GPS coordinates in the reconstruction reference frame
    "gps_dop": METERS,          # GPS accuracy in meters
    "orientation": NUMBER,      # EXIF orientation tag (can be 1, 3, 6 or 8)
    "capture_time": SECONDS     # Capture time as a UNIX timestamp
}

POINT: {
    "coordinates": [X, Y, Z],      # Estimated position of the point
    "color": [R, G, B],            # Color of the point
}
"""


def import_camera(
        opensfm_camera: Dict[str, float],
        name: Optional[str] = None
) -> kapture.Camera:
    # opensfm_camera['projection_type'] can be perspective, brown, fisheye or equirectangular
    if 'perspective' == opensfm_camera['projection_type']:
        # convert to CameraType.RADIAL [w, h, f, cx, cy, k1, k2]
        # missing principal point, just fake it at image center
        largest_side_in_pixel = float(max(opensfm_camera['width'], opensfm_camera['height']))
        camera_params = [
            # w, h:
            opensfm_camera['width'], opensfm_camera['height'],
            # f: The focal length provided by the EXIF metadata divided by the sensor width
            opensfm_camera['focal'] * largest_side_in_pixel,
            # cx, cy: no principal point, guess one at image center
            opensfm_camera['width'] / 2, opensfm_camera['height'] / 2,
            # k1, k2
            opensfm_camera.get('k1', 0.0), opensfm_camera.get('k2', 0.0),
        ]
        return kapture.Camera(
            camera_type=kapture.CameraType.RADIAL,
            camera_params=camera_params,
            name=name
        )

    else:
        raise ValueError(f'unable to convert camera of type {opensfm_camera["projection_type"]}')


def import_opensfm(
        opensfm_rootdir: str,
        kapture_rootdir: str,
        force_overwrite_existing: bool = False,
        images_import_method: TransferAction = TransferAction.copy
) -> None:
    disable_tqdm = logger.getEffectiveLevel() != logging.INFO
    # load reconstruction
    opensfm_reconstruction_filepath = path.join(opensfm_rootdir, 'reconstruction.json')
    with open(opensfm_reconstruction_filepath, 'rt') as f:
        opensfm_reconstruction = json.load(f)
    # remove the single list @ root
    opensfm_reconstruction = opensfm_reconstruction[0]

    # prepare space for output
    os.makedirs(kapture_rootdir, exist_ok=True)
    delete_existing_kapture_files(kapture_rootdir, force_erase=force_overwrite_existing)

    # import cameras
    kapture_sensors = kapture.Sensors()
    assert 'cameras' in opensfm_reconstruction
    # import cameras
    for osfm_camera_id, osfm_camera in opensfm_reconstruction['cameras'].items():
        camera = import_camera(osfm_camera, name=osfm_camera_id)
        kapture_sensors[osfm_camera_id] = camera

    # import shots
    logger.info('importing images and trajectories ...')
    kapture_images = kapture.RecordsCamera()
    kapture_trajectories = kapture.Trajectories()
    opensfm_image_dirpath = path.join(opensfm_rootdir, 'images')
    assert 'shots' in opensfm_reconstruction
    image_timestamps, image_sensors = {}, {}  # used later to retrieve the timestamp of an image.
    for timestamp, (image_filename, shot) in enumerate(opensfm_reconstruction['shots'].items()):
        sensor_id = shot['camera']
        image_timestamps[image_filename] = timestamp
        image_sensors[image_filename] = sensor_id
        # in OpenSfm, (sensor, timestamp) is not unique.
        rotation_vector = shot['rotation']
        q = quaternion.from_rotation_vector(rotation_vector)
        translation = shot['translation']
        # capture_time = shot['capture_time'] # may be invalid
        # gps_position = shot['gps_position']
        kapture_images[timestamp, sensor_id] = image_filename
        kapture_trajectories[timestamp, sensor_id] = kapture.PoseTransform(r=q, t=translation)

    # copy image files
    filename_list = [f for _, _, f in kapture.flatten(kapture_images)]
    import_record_data_from_dir_auto(
        source_record_dirpath=opensfm_image_dirpath,
        destination_kapture_dirpath=kapture_rootdir,
        filename_list=filename_list,
        copy_strategy=images_import_method)

    # gps from pre-extracted exif, in exif/image_name.jpg.exif
    kapture_gnss = None
    opensfm_exif_dirpath = path.join(opensfm_rootdir, 'exif')
    opensfm_exif_suffix = '.exif'
    if path.isdir(opensfm_exif_dirpath):
        logger.info('importing GNSS from exif ...')
        camera_ids = set(image_sensors.values())
        # add a gps sensor for each camera
        map_cam_to_gnss_sensor = {cam_id: 'GPS_' + cam_id for cam_id in camera_ids}
        for gnss_id in map_cam_to_gnss_sensor.values():
            kapture_sensors[gnss_id] = kapture.Sensor(sensor_type='gnss', sensor_params=['EPSG:4326'])
        # build epsg_code for all cameras
        kapture_gnss = kapture.RecordsGnss()
        opensfm_exif_filepath_list = (path.join(dirpath, filename)
                                      for dirpath, _, filename_list in os.walk(opensfm_exif_dirpath)
                                      for filename in filename_list
                                      if filename.endswith(opensfm_exif_suffix))
        for opensfm_exif_filepath in tqdm(opensfm_exif_filepath_list, disable=disable_tqdm):
            image_filename = path.relpath(opensfm_exif_filepath, opensfm_exif_dirpath)[:-len(opensfm_exif_suffix)]
            image_timestamp = image_timestamps[image_filename]
            image_sensor_id = image_sensors[image_filename]
            gnss_timestamp = image_timestamp
            gnss_sensor_id = map_cam_to_gnss_sensor[image_sensor_id]
            with open(opensfm_exif_filepath, 'rt') as f:
                js_root = json.load(f)
                if 'gps' not in js_root:
                    logger.warning(f'NO GPS data in "{opensfm_exif_filepath}"')
                    continue

                gps_coords = {
                    'x': js_root['gps']['longitude'],
                    'y': js_root['gps']['latitude'],
                    'z': js_root['gps'].get('altitude', 0.0),
                    'dop': js_root['gps'].get('dop', 0),
                    'utc': 0,
                }
                logger.debug(f'found GPS data for ({gnss_timestamp}, {gnss_sensor_id}) in "{opensfm_exif_filepath}"')
                kapture_gnss[gnss_timestamp, gnss_sensor_id] = kapture.RecordGnss(**gps_coords)

    # import features (keypoints + descriptors)
    kapture_keypoints = None  # kapture.Keypoints(type_name='opensfm', dsize=4, dtype=np.float64)
    kapture_descriptors = None  # kapture.Descriptors(type_name='opensfm', dsize=128, dtype=np.uint8)
    opensfm_features_dirpath = path.join(opensfm_rootdir, 'features')
    opensfm_features_suffix = '.features.npz'
    if path.isdir(opensfm_features_dirpath):
        logger.info('importing keypoints and descriptors ...')
        opensfm_features_file_list = (path.join(dp, fn)
                                      for dp, _, fs in os.walk(opensfm_features_dirpath) for fn in fs)
        opensfm_features_file_list = (filepath
                                      for filepath in opensfm_features_file_list
                                      if filepath.endswith(opensfm_features_suffix))
        for opensfm_feature_filename in tqdm(opensfm_features_file_list, disable=disable_tqdm):
            image_filename = path.relpath(opensfm_feature_filename, opensfm_features_dirpath)[
                             :-len(opensfm_features_suffix)]
            opensfm_image_features = np.load(opensfm_feature_filename)
            opensfm_image_keypoints = opensfm_image_features['points']
            opensfm_image_descriptors = opensfm_image_features['descriptors']
            logger.debug(f'parsing keypoints and descriptors in {opensfm_feature_filename}')
            if kapture_keypoints is None:
                # print(type(opensfm_image_keypoints.dtype))
                # HAHOG = Hessian Affine feature point detector + HOG descriptor
                kapture_keypoints = kapture.Keypoints(
                    type_name='HessianAffine',
                    dsize=opensfm_image_keypoints.shape[1],
                    dtype=opensfm_image_keypoints.dtype)
            if kapture_descriptors is None:
                kapture_descriptors = kapture.Descriptors(
                    type_name='HOG',
                    dsize=opensfm_image_descriptors.shape[1],
                    dtype=opensfm_image_descriptors.dtype)

            # convert keypoints file
            keypoint_filpath = kapture.io.features.get_features_fullpath(
                data_type=kapture.Keypoints, kapture_dirpath=kapture_rootdir, image_filename=image_filename)
            kapture.io.features.image_keypoints_to_file(
                filepath=keypoint_filpath, image_keypoints=opensfm_image_keypoints)
            # register the file
            kapture_keypoints.add(image_filename)

            # convert descriptors file
            descriptor_filpath = kapture.io.features.get_features_fullpath(
                data_type=kapture.Descriptors, kapture_dirpath=kapture_rootdir, image_filename=image_filename)
            kapture.io.features.image_descriptors_to_file(
                filepath=descriptor_filpath, image_descriptors=opensfm_image_descriptors)
            # register the file
            kapture_descriptors.add(image_filename)

    # import matches
    kapture_matches = kapture.Matches()
    opensfm_matches_suffix = '_matches.pkl.gz'
    opensfm_matches_dirpath = path.join(opensfm_rootdir, 'matches')
    if path.isdir(opensfm_matches_dirpath):
        logger.info('importing matches ...')
        opensfm_matches_file_list = (path.join(dp, fn)
                                     for dp, _, fs in os.walk(opensfm_matches_dirpath) for fn in fs)
        opensfm_matches_file_list = (filepath
                                     for filepath in opensfm_matches_file_list
                                     if filepath.endswith(opensfm_matches_suffix))

        for opensfm_matches_filename in tqdm(opensfm_matches_file_list, disable=disable_tqdm):
            image_filename_1 = path.relpath(opensfm_matches_filename, opensfm_matches_dirpath)[
                               :-len(opensfm_matches_suffix)]
            logger.debug(f'parsing mathes in {image_filename_1}')
            with gzip.open(opensfm_matches_filename, 'rb') as f:
                opensfm_matches = pickle.load(f)
                for image_filename_2, opensfm_image_matches in opensfm_matches.items():
                    image_pair = (image_filename_1, image_filename_2)
                    # register the pair to kapture
                    kapture_matches.add(*image_pair)
                    # convert the bin file to kapture
                    kapture_matches_filepath = kapture.io.features.get_matches_fullpath(
                        image_filename_pair=image_pair,
                        kapture_dirpath=kapture_rootdir)
                    kapture_image_matches = np.hstack([
                        opensfm_image_matches.astype(np.float64),
                        # no macthes scoring = assume all to one
                        np.ones(shape=(opensfm_image_matches.shape[0], 1), dtype=np.float64)])
                    kapture.io.features.image_matches_to_file(kapture_matches_filepath, kapture_image_matches)

    # import 3-D points
    if 'points' in opensfm_reconstruction:
        logger.info('importing points 3-D')
        opensfm_points = opensfm_reconstruction['points']
        points_data = []
        for point_id in sorted(opensfm_points):
            point_data = opensfm_points[point_id]
            point_data = point_data['coordinates'] + point_data['color']
            points_data.append(point_data)
        kapture_points = kapture.Points3d(points_data)
    else:
        kapture_points = None

    # saving kapture csv files
    logger.info('saving kapture files')
    kapture_data = kapture.Kapture(
        sensors=kapture_sensors,
        records_camera=kapture_images,
        records_gnss=kapture_gnss,
        trajectories=kapture_trajectories,
        keypoints=kapture_keypoints,
        descriptors=kapture_descriptors,
        matches=kapture_matches,
        points3d=kapture_points
    )
    kapture.io.csv.kapture_to_dir(dirpath=kapture_rootdir, kapture_data=kapture_data)
