# Copyright 2020-present NAVER Corp. Under BSD 3-clause license

"""
This script exports data from kapture to COLMAP database and/or reconstruction files.
"""

import logging
import os
import os.path as path
from typing import Optional
import warnings

# kapture
import kapture
from kapture.core.Trajectories import rigs_remove_inplace
import kapture.io.csv as csv
import kapture.io.structure
import kapture.algo.merge_keep_ids

# local
from .database import COLMAPDatabase
from .database_extra import is_colmap_db_empty, kapture_to_colmap, get_colmap_image_ids_from_db,\
    get_colmap_camera_ids_from_db
from .export_colmap_rigs import export_colmap_rig
from .export_colmap_reconstruction import export_to_colmap_txt

logger = logging.getLogger('colmap')


def export_colmap(kapture_dirpath: str,
                  colmap_database_filepath: str,
                  colmap_reconstruction_dirpath: Optional[str],
                  colmap_rig_filepath: str = None,
                  force_overwrite_existing: bool = False) -> None:
    """
    Exports kapture data to colmap database and or reconstruction text files.

    :param kapture_dirpath: kapture top directory
    :param colmap_database_filepath: path to colmap database file
    :param colmap_reconstruction_dirpath: path to colmap reconstruction directory
    :param colmap_rig_filepath: path to colmap rig file
    :param force_overwrite_existing: Silently overwrite colmap files if already exists.
    """

    os.makedirs(path.dirname(colmap_database_filepath), exist_ok=True)
    if colmap_reconstruction_dirpath:
        os.makedirs(colmap_reconstruction_dirpath, exist_ok=True)

    assert colmap_database_filepath
    if path.isfile(colmap_database_filepath):
        to_delete = force_overwrite_existing or (input(
            'database file already exist, would you like to delete it ? [y/N]').lower() == 'y')
        if to_delete:
            logger.info('deleting already existing {}'.format(colmap_database_filepath))
            os.remove(colmap_database_filepath)

    logger.info('creating colmap database in {}'.format(colmap_database_filepath))
    db = COLMAPDatabase.connect(colmap_database_filepath)
    if not is_colmap_db_empty(db):
        raise ValueError('the existing colmap database is not empty : {}'.format(colmap_database_filepath))

    logger.info('loading kapture files...')
    kapture_data = csv.kapture_from_dir(kapture_dirpath)
    assert isinstance(kapture_data, kapture.Kapture)

    # COLMAP does not fully support rigs.
    if kapture_data.rigs is not None:
        # make sure, rigs are not used in trajectories.
        logger.info('remove rigs notation.')
        rigs_remove_inplace(kapture_data.trajectories, kapture_data.rigs)

    # write colmap database
    kapture_to_colmap(kapture_data, kapture_dirpath, db)

    if colmap_reconstruction_dirpath:
        # create text files
        colmap_camera_ids = get_colmap_camera_ids_from_db(db, kapture_data.records_camera)
        colmap_image_ids = get_colmap_image_ids_from_db(db)
        export_to_colmap_txt(
            colmap_reconstruction_dirpath=colmap_reconstruction_dirpath,
            kapture_data=kapture_data,
            kapture_dirpath=kapture_dirpath,
            colmap_camera_ids=colmap_camera_ids,
            colmap_image_ids=colmap_image_ids)
        if colmap_rig_filepath:
            try:
                if kapture_data.rigs is None:
                    raise ValueError('No rig to export.')
                export_colmap_rigs(colmap_rig_filepath,
                                   kapture_data.rigs,
                                   kapture_data.records_camera,
                                   colmap_camera_ids)
            except Exception as e:
                warnings.warn(e)
                logger.warning(e)

    db.close()
