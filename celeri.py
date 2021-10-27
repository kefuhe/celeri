import addict
import copy
import datetime
import json
import meshio
import scipy
import pyproj
import matplotlib.pyplot as plt
import warnings
import numpy as np
import pandas as pd
import okada_wrapper
import cutde.halfspace as cutde_halfspace
from loguru import logger
from tqdm.notebook import tqdm
from typing import List, Dict, Tuple

import celeri
import celeri_closure
from celeri_util import sph2cart

# Global constants
GEOID = pyproj.Geod(ellps="WGS84")
KM2M = 1.0e3
M2MM = 1.0e3
RADIUS_EARTH = np.float64((GEOID.a + GEOID.b) / 2)
N_MESH_DIM = 3

# Set up logging to file only
RUN_NAME = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
logger.remove()  # Remove any existing loggers includeing default stderr
logger.add(RUN_NAME + ".log")
logger.info("RUN_NAME: " + RUN_NAME)


def read_data(command_file_name):
    # Read command data
    with open(command_file_name, "r") as f:
        command = json.load(f)
    command = addict.Dict(command)  # Convert to dot notation dictionary

    # Read segment data
    segment = pd.read_csv(command.segment_file_name)
    segment = segment.loc[:, ~segment.columns.str.match("Unnamed")]

    # Read block data
    block = pd.read_csv(command.block_file_name)
    block = block.loc[:, ~block.columns.str.match("Unnamed")]

    # Read mesh data - List of dictionary version
    with open(command.mesh_parameters_file_name, "r") as f:
        mesh_param = json.load(f)

    meshes = []
    for i in range(len(mesh_param)):
        meshes.append(addict.Dict())
        meshes[i].meshio_object = meshio.read(mesh_param[i]["mesh_filename"])
        meshes[i].verts = meshes[i].meshio_object.get_cells_type("triangle")
        # Expand mesh coordinates
        meshes[i].lon1 = meshes[i].meshio_object.points[meshes[i].verts[:, 0], 0]
        meshes[i].lon2 = meshes[i].meshio_object.points[meshes[i].verts[:, 1], 0]
        meshes[i].lon3 = meshes[i].meshio_object.points[meshes[i].verts[:, 2], 0]
        meshes[i].lat1 = meshes[i].meshio_object.points[meshes[i].verts[:, 0], 1]
        meshes[i].lat2 = meshes[i].meshio_object.points[meshes[i].verts[:, 1], 1]
        meshes[i].lat3 = meshes[i].meshio_object.points[meshes[i].verts[:, 2], 1]
        meshes[i].dep1 = meshes[i].meshio_object.points[meshes[i].verts[:, 0], 2]
        meshes[i].dep2 = meshes[i].meshio_object.points[meshes[i].verts[:, 1], 2]
        meshes[i].dep3 = meshes[i].meshio_object.points[meshes[i].verts[:, 2], 2]
        meshes[i].centroids = np.mean(
            meshes[i].meshio_object.points[meshes[i].verts, :], axis=1
        )
        # Cartesian coordinates in meters
        meshes[i].x1, meshes[i].y1, meshes[i].z1 = sph2cart(
            meshes[i].lon1,
            meshes[i].lat1,
            celeri.RADIUS_EARTH + KM2M * meshes[i].dep1,
        )
        meshes[i].x2, meshes[i].y2, meshes[i].z2 = sph2cart(
            meshes[i].lon2,
            meshes[i].lat2,
            celeri.RADIUS_EARTH + KM2M * meshes[i].dep2,
        )
        meshes[i].x3, meshes[i].y3, meshes[i].z3 = sph2cart(
            meshes[i].lon3,
            meshes[i].lat3,
            celeri.RADIUS_EARTH + KM2M * meshes[i].dep3,
        )

        # Cartesian triangle centroids
        meshes[i].x_centroid = (meshes[i].x1 + meshes[i].x2 + meshes[i].x3) / 3.0
        meshes[i].y_centroid = (meshes[i].y1 + meshes[i].y2 + meshes[i].y3) / 3.0
        meshes[i].z_centroid = (meshes[i].z1 + meshes[i].z2 + meshes[i].z3) / 3.0

        # Cross products for orientations
        tri_leg1 = np.transpose(
            [
                np.deg2rad(meshes[i].lon2 - meshes[i].lon1),
                np.deg2rad(meshes[i].lat2 - meshes[i].lat1),
                (1 + KM2M * meshes[i].dep2 / celeri.RADIUS_EARTH)
                - (1 + KM2M * meshes[i].dep1 / celeri.RADIUS_EARTH),
            ]
        )
        tri_leg2 = np.transpose(
            [
                np.deg2rad(meshes[i].lon3 - meshes[i].lon1),
                np.deg2rad(meshes[i].lat3 - meshes[i].lat1),
                (1 + KM2M * meshes[i].dep3 / celeri.RADIUS_EARTH)
                - (1 + KM2M * meshes[i].dep1 / celeri.RADIUS_EARTH),
            ]
        )
        meshes[i].nv = np.cross(tri_leg1, tri_leg2)
        azimuth, elevation, r = celeri.cart2sph(
            meshes[i].nv[:, 0],
            meshes[i].nv[:, 1],
            meshes[i].nv[:, 2],
        )
        meshes[i].strike = celeri.wrap2360(-np.rad2deg(azimuth))
        meshes[i].dip = 90 - np.rad2deg(elevation)
        meshes[i].dip_flag = meshes[i].dip != 90
        meshes[i].smoothing_weight = mesh_param[i]["smoothing_weight"]
        meshes[i].n_eigenvalues = mesh_param[i]["n_eigenvalues"]
        meshes[i].edge_constraints = mesh_param[i]["edge_constraints"]

    # Read station data
    if (
        not command.__contains__("station_file_name")
        or len(command.station_file_name) == 0
    ):
        station = pd.DataFrame(
            columns=[
                "lon",
                "lat",
                "corr",
                "other1",
                "name",
                "east_vel",
                "north_vel",
                "east_sig",
                "north_sig",
                "flag",
                "up_vel",
                "up_sig",
                "east_adjust",
                "north_adjust",
                "up_adjust",
                "depth",
                "x",
                "y",
                "z",
                "block_label",
            ]
        )
    else:
        station = pd.read_csv(command.station_file_name)
        station = station.loc[:, ~station.columns.str.match("Unnamed")]

    # Read Mogi source data
    if not command.__contains__("mogi_file_name") or len(command.mogi_file_name) == 0:
        mogi = pd.DataFrame(
            columns=[
                "name",
                "lon",
                "lat",
                "depth",
                "volume_change_flag",
                "volume_change",
                "volume_change_sig",
            ]
        )
    else:
        mogi = pd.read_csv(command.mogi_file_name)
        mogi = mogi.loc[:, ~mogi.columns.str.match("Unnamed")]

    # Read SAR data
    if not command.__contains__("sar_file_name") or len(command.sar_file_name) == 0:
        sar = pd.DataFrame(
            columns=[
                "lon",
                "lat",
                "depth",
                "line_of_sight_change_val",
                "line_of_sight_change_sig",
                "look_vector_x",
                "look_vector_y",
                "look_vector_z",
                "reference_point_x",
                "reference_point_y",
            ]
        )

    else:
        sar = pd.read_csv(command.sar_file_name)
        sar = sar.loc[:, ~sar.columns.str.match("Unnamed")]
    return command, segment, block, meshes, station, mogi, sar


def cart2sph(x, y, z):
    azimuth = np.arctan2(y, x)
    elevation = np.arctan2(z, np.sqrt(x ** 2 + y ** 2))
    r = np.sqrt(x ** 2 + y ** 2 + z ** 2)
    return azimuth, elevation, r


def wrap2360(lon):
    lon[np.where(lon < 0.0)] += 360.0
    return lon


def process_station(station, command):
    if command.unit_sigmas == "yes":  # Assign unit uncertainties, if requested
        station.east_sig = np.ones_like(station.east_sig)
        station.north_sig = np.ones_like(station.north_sig)
        station.up_sig = np.ones_like(station.up_sig)

    station["depth"] = np.zeros_like(station.lon)
    station["x"], station["y"], station["z"] = sph2cart(
        station.lon, station.lat, celeri.RADIUS_EARTH
    )
    station = station.drop(np.where(station.flag == 0)[0])
    station = station.reset_index(drop=True)
    return station


def locking_depth_manager(segment, command):
    """
    This function assigns the locking depths given in the command file to any
    segment that has the same locking depth flag.  Segments with flag =
    0, 1 are untouched.
    """
    segment = segment.copy(deep=True)
    segment.locking_depth.values[
        segment.locking_depth_flag == 2
    ] = command.locking_depth_flag2
    segment.locking_depth.values[
        segment.locking_depth_flag == 3
    ] = command.locking_depth_flag3
    segment.locking_depth.values[
        segment.locking_depth_flag == 4
    ] = command.locking_depth_flag4
    segment.locking_depth.values[
        segment.locking_depth_flag == 5
    ] = command.locking_depth_flag5

    if command.locking_depth_override_flag == "yes":
        segment.locking_depth.values = command.locking_depth_override_value
    return segment


def zero_mesh_segment_locking_depth(segment, meshes):
    """
    This function sets the locking depths of any segments that trace
    a mesh to zero, so that they have no rectangular elastic strain
    contribution, as the elastic strain is accounted for by the mesh.

    To have its locking depth set to zero, the segment's patch_flag
    and patch_file_name fields must not be equal to zero but also
    less than the number of available mesh files.
    """
    segment = segment.copy(deep=True)
    toggle_off = np.where(
        (segment.patch_flag != 0)
        & (segment.patch_file_name != 0)
        & (segment.patch_file_name <= len(meshes))
    )[0]
    segment.locking_depth.values[toggle_off] = 0
    return segment


def order_endpoints_sphere(segment):
    """
    Endpoint ordering function, placing west point first.
    This converts the endpoint coordinates from spherical to Cartesian,
    then takes the cross product to test for ordering (i.e., a positive z
    component of cross(point1, point2) means that point1 is the western
    point). This method works for both (-180, 180) and (0, 360) longitude
    conventions.
    BJM: Not sure why cross product approach was definitely not working in
    python so I revereted to relative longitude check which sould be fine because
    we're always in 0-360 space.
    """
    segment_copy = copy.deepcopy(segment)
    endpoints1 = np.transpose(np.array([segment.x1, segment.y1, segment.z1]))
    endpoints2 = np.transpose(np.array([segment.x2, segment.y2, segment.z2]))
    cross_product = np.cross(endpoints1, endpoints2)
    swap_endpoint_idx = np.where(cross_product[:, 2] < 0)
    segment_copy.lon1.values[swap_endpoint_idx] = segment.lon2.values[swap_endpoint_idx]
    segment_copy.lat1.values[swap_endpoint_idx] = segment.lat2.values[swap_endpoint_idx]
    segment_copy.lon2.values[swap_endpoint_idx] = segment.lon1.values[swap_endpoint_idx]
    segment_copy.lat2.values[swap_endpoint_idx] = segment.lat1.values[swap_endpoint_idx]
    return segment_copy


def segment_centroids(segment):
    """Calculate segment centroids."""
    segment["centroid_x"] = np.zeros_like(segment.lon1)
    segment["centroid_y"] = np.zeros_like(segment.lon1)
    segment["centroid_z"] = np.zeros_like(segment.lon1)
    segment["centroid_lon"] = np.zeros_like(segment.lon1)
    segment["centroid_lat"] = np.zeros_like(segment.lon1)

    for i in range(len(segment)):
        segment_forward_azimuth, _, _ = celeri.GEOID.inv(
            segment.lon1[i], segment.lat1[i], segment.lon2[i], segment.lat2[i]
        )
        segment_down_dip_azimuth = segment_forward_azimuth + 90.0 * np.sign(
            np.cos(np.deg2rad(segment.dip[i]))
        )
        azx = (segment.y2[i] - segment.y1[i]) / (segment.x2[i] - segment.x1[i])
        azx = np.arctan(-1.0 / azx)  # TODO: MAKE THIS VARIABLE NAME DESCRIPTIVE
        segment.centroid_z.values[i] = (
            segment.locking_depth[i] - segment.burial_depth[i]
        ) / 2.0
        segment_down_dip_distance = segment.centroid_z[i] / np.abs(
            np.tan(np.deg2rad(segment.dip[i]))
        )
        (
            segment.centroid_lon.values[i],
            segment.centroid_lat.values[i],
            _,
        ) = celeri.GEOID.fwd(
            segment.mid_lon[i],
            segment.mid_lat[i],
            segment_down_dip_azimuth,
            segment_down_dip_distance,
        )
        segment.centroid_x.values[i] = segment.mid_x[i] + np.sign(
            np.cos(np.deg2rad(segment.dip[i]))
        ) * segment_down_dip_distance * np.cos(azx)
        segment.centroid_y.values[i] = segment.mid_y[i] + np.sign(
            np.cos(np.deg2rad(segment.dip[i]))
        ) * segment_down_dip_distance * np.sin(azx)
    segment.centroid_lon.values[segment.centroid_lon < 0.0] += 360.0
    return segment


def process_segment(segment, command, meshes):
    """
    Add derived fields to segment dataframe
    """

    segment["length"] = np.zeros(len(segment))
    for i in range(len(segment)):
        _, _, segment.length.values[i] = celeri.GEOID.inv(
            segment.lon1[i], segment.lat1[i], segment.lon2[i], segment.lat2[i]
        )  # Segment length in meters

    segment["x1"], segment["y1"], segment["z1"] = sph2cart(
        segment.lon1, segment.lat1, celeri.RADIUS_EARTH
    )
    segment["x2"], segment["y2"], segment["z2"] = sph2cart(
        segment.lon2, segment.lat2, celeri.RADIUS_EARTH
    )

    segment = celeri.order_endpoints_sphere(segment)

    # This calculation needs to account for the periodic nature of longitude.
    # Calculate the periodic longitudinal separation.
    # @BJM: Is this better done with GEIOD?
    sep = segment.lon2 - segment.lon1
    periodic_lon_separation = np.where(
        sep > 180, sep - 360, np.where(sep < -180, sep + 360, sep)
    )
    segment["mid_lon_plate_carree"] = (
        segment.lon1.values + periodic_lon_separation / 2.0
    )

    # No worries for latitude because there's no periodicity.
    segment["mid_lat_plate_carree"] = (segment.lat1.values + segment.lat2.values) / 2.0
    segment["mid_lon"] = np.zeros_like(segment.lon1)
    segment["mid_lat"] = np.zeros_like(segment.lon1)

    for i in range(len(segment)):
        segment.mid_lon.values[i], segment.mid_lat.values[i] = celeri.GEOID.npts(
            segment.lon1[i], segment.lat1[i], segment.lon2[i], segment.lat2[i], 1
        )[0]
    segment.mid_lon.values[segment.mid_lon < 0.0] += 360.0

    segment["mid_x"], segment["mid_y"], segment["mid_z"] = sph2cart(
        segment.mid_lon, segment.mid_lat, celeri.RADIUS_EARTH
    )
    segment = celeri.locking_depth_manager(segment, command)
    segment = celeri.zero_mesh_segment_locking_depth(segment, meshes)
    # segment.locking_depth.values = PatchLDtoggle(segment.locking_depth, segment.patch_file_name, segment.patch_flag, Command.patchFileNames) % Set locking depth to zero on segments that are associated with patches # TODO: Write this after patches are read in.
    segment = celeri.segment_centroids(segment)
    return segment


def polygon_area(x, y):
    """
    From: https://newbedev.com/calculate-area-of-polygon-given-x-y-coordinates
    """
    return 0.5 * np.abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))


def assign_block_labels(segment, station, block, mogi, sar):
    """
    Ben Thompson's implementation of the half edge approach to the
    block labeling problem and east/west assignment.
    """
    # segment = split_segments_crossing_meridian(segment)

    np_segments = np.zeros((len(segment), 2, 2))
    np_segments[:, 0, 0] = segment.lon1.to_numpy()
    np_segments[:, 1, 0] = segment.lon2.to_numpy()
    np_segments[:, 0, 1] = segment.lat1.to_numpy()
    np_segments[:, 1, 1] = segment.lat2.to_numpy()

    closure = celeri_closure.run_block_closure(np_segments)
    labels = celeri_closure.get_segment_labels(closure)

    segment["west_labels"] = labels[:, 0]
    segment["east_labels"] = labels[:, 1]

    # Check for unprocessed indices
    unprocessed_indices = np.union1d(
        np.where(segment["east_labels"] < 0),
        np.where(segment["west_labels"] < 0),
    )
    if len(unprocessed_indices) > 0:
        print("Found unproccessed indices")

    # Find relative areas of each block to identify an external block
    block["area_plate_carree"] = -1 * np.ones(len(block))
    for i in range(closure.n_polygons()):
        vs = closure.polygons[i].vertices
        # TODO: can we use closure.polygons[i].area_steradians here?
        block.area_plate_carree.values[i] = celeri.polygon_area(vs[:, 0], vs[:, 1])

    # Assign block labels points to block interior points
    block["block_label"] = closure.assign_points(
        block.interior_lon.to_numpy(), block.interior_lat.to_numpy()
    )

    # I copied this from the bottom of:
    # https://stackoverflow.com/questions/39992502/rearrange-rows-of-pandas-dataframe-based-on-list-and-keeping-the-order
    # and I definitely don't understand it all but emperically it seems to work.
    block = (
        block.set_index(block.block_label, append=True)
        .sort_index(level=1)
        .reset_index(1, drop=True)
    )
    block = block.reset_index()
    block = block.loc[:, ~block.columns.str.match("index")]

    # Assign block labels to GPS stations
    if not station.empty:
        station["block_label"] = closure.assign_points(
            station.lon.to_numpy(), station.lat.to_numpy()
        )

    # Assign block labels to SAR locations
    if not sar.empty:
        sar["block_label"] = closure.assign_points(
            sar.lon.to_numpy(), sar.lat.to_numpy()
        )

    # Assign block labels to Mogi sources
    if not mogi.empty:
        mogi["block_label"] = closure.assign_points(
            mogi.lon.to_numpy(), mogi.lat.to_numpy()
        )

    return closure, block


def great_circle_latitude_find(lon1, lat1, lon2, lat2, lon):
    """
    Determines latitude as a function of longitude along a great circle.
    LAT = gclatfind(LON1, LAT1, LON2, LAT2, LON) finds the latitudes of points of
    specified LON that lie along the great circle defined by endpoints LON1, LAT1
    and LON2, LAT2. Angles should be passed as degrees.
    """
    lon1 = np.deg2rad(lon1)
    lat1 = np.deg2rad(lat1)
    lon2 = np.deg2rad(lon2)
    lat2 = np.deg2rad(lat2)
    lon = np.deg2rad(lon)
    lat = np.arctan(
        np.tan(lat1) * np.sin(lon - lon2) / np.sin(lon1 - lon2)
        - np.tan(lat2) * np.sin(lon - lon1) / np.sin(lon1 - lon2)
    )
    return lat


def process_sar(sar, command):
    """
    Preprocessing of SAR data.
    """
    if sar.empty:
        sar["depth"] = np.zeros_like(sar.lon)

        # Set the uncertainties to reflect the weights specified in the command file
        # In constructing the data weight vector, the value is 1./Sar.dataSig.^2, so
        # the adjustment made here is sar.dataSig / np.sqrt(command.sarWgt)
        sar.line_of_sight_change_sig = sar.line_of_sight_change_sig / np.sqrt(
            command.sar_weight
        )
        sar["x"], sar["y"], sar["z"] = sph2cart(sar.lon, sar.lat, celeri.RADIUS_EARTH)
        sar["block_label"] = -1 * np.ones_like(sar.x)
    else:
        sar["dep"] = []
        sar["x"] = []
        sar["y"] = []
        sar["x"] = []
        sar["block_label"] = []
    return sar


def merge_geodetic_data(assembly, station, sar):
    """
    Merge GPS and InSAR data to a single assembly object
    """
    assembly.data.n_stations = len(station)
    assembly.data.n_sar = len(sar)
    assembly.data.east_vel = station.east_vel.to_numpy()
    assembly.sigma.east_sig = station.east_sig.to_numpy()
    assembly.data.north_vel = station.north_vel.to_numpy()
    assembly.sigma.north_sig = station.north_sig.to_numpy()
    assembly.data.up_vel = station.up_vel.to_numpy()
    assembly.sigma.up_sig = station.up_sig.to_numpy()
    assembly.data.sar_line_of_sight_change_val = sar.line_of_sight_change_val.to_numpy()
    assembly.sigma.sar_line_of_sight_change_sig = (
        sar.line_of_sight_change_sig.to_numpy()
    )
    assembly.data.lon = np.concatenate((station.lon.to_numpy(), sar.lon.to_numpy()))
    assembly.data.lat = np.concatenate((station.lat.to_numpy(), sar.lat.to_numpy()))
    assembly.data.depth = np.concatenate(
        (station.depth.to_numpy(), sar.depth.to_numpy())
    )
    assembly.data.x = np.concatenate((station.x.to_numpy(), sar.x.to_numpy()))
    assembly.data.y = np.concatenate((station.y.to_numpy(), sar.y.to_numpy()))
    assembly.data.z = np.concatenate((station.z.to_numpy(), sar.z.to_numpy()))
    assembly.data.block_label = np.concatenate(
        (station.block_label.to_numpy(), sar.block_label.to_numpy())
    )
    assembly.index.sar_coordinate_idx = np.arange(
        len(station), len(station) + len(sar)
    )  # TODO: Not sure this is correct
    return assembly


def euler_pole_covariance_to_rotation_vector_covariance(
    omega_x, omega_y, omega_z, euler_pole_covariance_all
):
    """
    This function takes the model parameter covariance matrix
    in terms of the Euler pole and rotation rate and linearly
    propagates them to rotation vector space.
    """
    omega_x_sig = np.zeros_like(omega_x)
    omega_y_sig = np.zeros_like(omega_y)
    omega_z_sig = np.zeros_like(omega_z)
    for i in range(len(omega_x)):
        x = omega_x[i]
        y = omega_y[i]
        z = omega_z[i]
        euler_pole_covariance_current = euler_pole_covariance_all[
            3 * i : 3 * (i + 1), 3 * i : 3 * (i + 1)
        ]

        """
        There may be cases where x, y and z are all zero.  This leads to /0 errors.  To avoid this  %%
        we check for these cases and Let A = b * I where b is a small constant (10^-4) and I is     %%
        the identity matrix
        """
        if (x == 0) and (y == 0):
            euler_to_cartsian_operator = 1e-4 * np.eye(
                3
            )  # Set a default small value for rotation vector uncertainty
        else:
            # Calculate the partial derivatives
            dlat_dx = (
                -z / (x ** 2 + y ** 2) ** (3 / 2) / (1 + z ** 2 / (x ** 2 + y ** 2)) * x
            )
            dlat_dy = (
                -z / (x ** 2 + y ** 2) ** (3 / 2) / (1 + z ** 2 / (x ** 2 + y ** 2)) * y
            )
            dlat_dz = (
                1 / (x ** 2 + y ** 2) ** (1 / 2) / (1 + z ** 2 / (x ** 2 + y ** 2))
            )
            dlon_dx = -y / x ** 2 / (1 + (y / x) ** 2)
            dlon_dy = 1 / x / (1 + (y / x) ** 2)
            dlon_dz = 0
            dmag_dx = x / np.sqrt(x ** 2 + y ** 2 + z ** 2)
            dmag_dy = y / np.sqrt(x ** 2 + y ** 2 + z ** 2)
            dmag_dz = z / np.sqrt(x ** 2 + y ** 2 + z ** 2)
            euler_to_cartsian_operator = np.array(
                [
                    [dlat_dx, dlat_dy, dlat_dz],
                    [dlon_dx, dlon_dy, dlon_dz],
                    [dmag_dx, dmag_dy, dmag_dz],
                ]
            )

        # Propagate the Euler pole covariance matrix to a rotation rate
        # covariance matrix
        rotation_vector_covariance = (
            np.linalg.inv(euler_to_cartsian_operator)
            * euler_pole_covariance_current
            * np.linalg.inv(euler_to_cartsian_operator).T
        )

        # Organized data for the return
        main_diagonal_values = np.diag(rotation_vector_covariance)
        omega_x_sig[i] = np.sqrt(main_diagonal_values[0])
        omega_y_sig[i] = np.sqrt(main_diagonal_values[1])
        omega_z_sig[i] = np.sqrt(main_diagonal_values[2])
    return omega_x_sig, omega_y_sig, omega_z_sig


def get_block_constraint_partials(block):
    """
    Partials for a priori block motion constraints.
    Essentially a set of eye(3) matrices
    """
    # apriori_block_idx = np.where(block.apriori_flag.to_numpy() == 1)[0]
    apriori_rotation_block_idx = np.where(block.rotation_flag.to_numpy() == 1)[0]

    operator = np.zeros((3 * len(apriori_rotation_block_idx), 3 * len(block)))
    for i in range(len(apriori_rotation_block_idx)):
        start_row = 3 * i
        start_column = 3 * apriori_rotation_block_idx[i]
        operator[start_row : start_row + 3, start_column : start_column + 3] = np.eye(3)
    return operator


def block_constraints(assembly: Dict, block: pd.DataFrame, command: Dict):
    """
    Applying a priori block motion constraints
    """
    block_constraint_partials = get_block_constraint_partials(block)
    assembly.index.block_constraints_idx = np.where(block.rotation_flag == 1)[0]

    assembly.data.n_block_constraints = len(assembly.index.block_constraints_idx)
    assembly.data.block_constraints = np.zeros(block_constraint_partials.shape[0])
    assembly.sigma.block_constraints = np.zeros(block_constraint_partials.shape[0])
    if assembly.data.n_block_constraints > 0:
        (
            assembly.data.block_constraints[0::3],
            assembly.data.block_constraints[1::3],
            assembly.data.block_constraints[2::3],
        ) = sph2cart(
            block.euler_lon[assembly.index.block_constraints_idx],
            block.euler_lat[assembly.index.block_constraints_idx],
            block.rotation_rate[assembly.index.block_constraints_idx],
        )
        euler_pole_covariance_all = np.diag(
            np.concatenate(
                (
                    np.deg2rad(
                        block.euler_lat_sig[assembly.index.block_constraints_idx]
                    ),
                    np.deg2rad(
                        block.euler_lon_sig[assembly.index.block_constraints_idx]
                    ),
                    np.deg2rad(
                        block.rotation_rate_sig[assembly.index.block_constraints_idx]
                    ),
                )
            )
        )
        (
            assembly.sigma.block_constraints[0::3],
            assembly.sigma.block_constraints[1::3],
            assembly.sigma.block_constraints[2::3],
        ) = euler_pole_covariance_to_rotation_vector_covariance(
            assembly.data.block_constraints[0::3],
            assembly.data.block_constraints[1::3],
            assembly.data.block_constraints[2::3],
            euler_pole_covariance_all,
        )
    assembly.sigma.block_constraint_weight = command.block_constraint_weight
    return assembly, block_constraint_partials


def get_cross_partials(vector):
    """
    Returns a linear operator R that when multiplied by
    vector a gives the cross product a cross b
    """
    return np.array(
        [
            [0, vector[2], -vector[1]],
            [-vector[2], 0, vector[0]],
            [vector[1], -vector[0], 0],
        ]
    )


def cartesian_vector_to_spherical_vector(vel_x, vel_y, vel_z, lon, lat):
    """
    This function transforms vectors from Cartesian to spherical components.
    Arguments:
        vel_x: array of x components of velocity
        vel_y: array of y components of velocity
        vel_z: array of z components of velocity
        lon: array of station longitudes
        lat: array of station latitudes
    Returned variables:
        vel_north: array of north components of velocity
        vel_east: array of east components of velocity
        vel_up: array of up components of velocity
    """
    projection_matrix = np.array(
        [
            [
                -np.sin(np.deg2rad(lat)) * np.cos(np.deg2rad(lon)),
                -np.sin(np.deg2rad(lat)) * np.sin(np.deg2rad(lon)),
                np.cos(np.deg2rad(lat)),
            ],
            [-np.sin(np.deg2rad(lon)), np.cos(np.deg2rad(lon)), 0],
            [
                -np.cos(np.deg2rad(lat)) * np.cos(np.deg2rad(lon)),
                -np.cos(np.deg2rad(lat)) * np.sin(np.deg2rad(lon)),
                -np.sin(np.deg2rad(lat)),
            ],
        ]
    )
    vel_north, vel_east, vel_up = np.dot(
        projection_matrix, np.array([vel_x, vel_y, vel_z])
    )
    return vel_north, vel_east, vel_up


def get_fault_slip_rate_partials(segment, block):
    """
    Calculate partial derivatives for slip rate constraints
    """
    n_segments = len(segment)
    n_blocks = len(block)
    fault_slip_rate_partials = np.zeros((3 * n_segments, 3 * n_blocks))
    for i in range(n_segments):
        # Project velocities from Cartesian to spherical coordinates at segment mid-points
        row_idx = 3 * i
        column_idx_east = 3 * segment.east_labels[i]
        column_idx_west = 3 * segment.west_labels[i]
        R = get_cross_partials([segment.mid_x[i], segment.mid_y[i], segment.mid_z[i]])
        (
            vel_north_to_omega_x,
            vel_east_to_omega_x,
            _,
        ) = cartesian_vector_to_spherical_vector(
            R[0, 0], R[1, 0], R[2, 0], segment.mid_lon[i], segment.mid_lat[i]
        )
        (
            vel_north_to_omega_y,
            vel_east_to_omega_y,
            _,
        ) = cartesian_vector_to_spherical_vector(
            R[0, 1], R[1, 1], R[2, 1], segment.mid_lon[i], segment.mid_lat[i]
        )
        (
            vel_north_to_omega_z,
            vel_east_to_omega_z,
            _,
        ) = cartesian_vector_to_spherical_vector(
            R[0, 2], R[1, 2], R[2, 2], segment.mid_lon[i], segment.mid_lat[i]
        )

        # Build unit vector for the fault
        # Projection on to fault strike
        segment_azimuth, _, _ = celeri.GEOID.inv(
            segment.lon1[i], segment.lat1[i], segment.lon2[i], segment.lat2[i]
        )  # TODO: Need to check this vs. matlab azimuth for consistency
        unit_x_parallel = np.cos(np.deg2rad(90 - segment_azimuth))
        unit_y_parallel = np.sin(np.deg2rad(90 - segment_azimuth))
        unit_x_perpendicular = np.sin(np.deg2rad(segment_azimuth - 90))
        unit_y_perpendicular = np.cos(np.deg2rad(segment_azimuth - 90))

        # Projection onto fault dip
        if segment.lat2[i] < segment.lat1[i]:
            unit_x_parallel = -unit_x_parallel
            unit_y_parallel = -unit_y_parallel
            unit_x_perpendicular = -unit_x_perpendicular
            unit_y_perpendicular = -unit_y_perpendicular

        # This is the logic for dipping vs. non-dipping faults
        # If fault is dipping make it so that the dip slip rate has a fault normal
        # component equal to the fault normal differential plate velocity.  This
        # is kinematically consistent in the horizontal but *not* in the vertical.
        if segment.dip[i] != 90:
            scale_factor = 1 / abs(np.cos(np.deg2rad(segment.dip[i])))
            slip_rate_matrix = np.array(
                [
                    [
                        unit_x_parallel * vel_east_to_omega_x
                        + unit_y_parallel * vel_north_to_omega_x,
                        unit_x_parallel * vel_east_to_omega_y
                        + unit_y_parallel * vel_north_to_omega_y,
                        unit_x_parallel * vel_east_to_omega_z
                        + unit_y_parallel * vel_north_to_omega_z,
                    ],
                    [
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_x
                            + unit_y_perpendicular * vel_north_to_omega_x
                        ),
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_y
                            + unit_y_perpendicular * vel_north_to_omega_y
                        ),
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_z
                            + unit_y_perpendicular * vel_north_to_omega_z
                        ),
                    ],
                    [0, 0, 0],
                ]
            )
        else:
            scale_factor = (
                -1
            )  # This is for consistency with the Okada convention for tensile faulting
            slip_rate_matrix = np.array(
                [
                    [
                        unit_x_parallel * vel_east_to_omega_x
                        + unit_y_parallel * vel_north_to_omega_x,
                        unit_x_parallel * vel_east_to_omega_y
                        + unit_y_parallel * vel_north_to_omega_y,
                        unit_x_parallel * vel_east_to_omega_z
                        + unit_y_parallel * vel_north_to_omega_z,
                    ],
                    [0, 0, 0],
                    [
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_x
                            + unit_y_perpendicular * vel_north_to_omega_x
                        ),
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_y
                            + unit_y_perpendicular * vel_north_to_omega_y
                        ),
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_z
                            + unit_y_perpendicular * vel_north_to_omega_z
                        ),
                    ],
                ]
            )

        fault_slip_rate_partials[
            row_idx : row_idx + 3, column_idx_east : column_idx_east + 3
        ] = slip_rate_matrix
        fault_slip_rate_partials[
            row_idx : row_idx + 3, column_idx_west : column_idx_west + 3
        ] = -slip_rate_matrix
    return fault_slip_rate_partials


def slip_rate_constraints(assembly, segment, block, command):
    logger.info("Isolating slip rate constraints")
    for i in range(len(segment.lon1)):
        if segment.ss_rate_flag[i] == 1:
            # "{:.2f}".format(segment.ss_rate[i])
            logger.info(
                "Strike-slip rate constraint on "
                + segment.name[i].strip()
                + ": rate = "
                + "{:.2f}".format(segment.ss_rate[i])
                + " (mm/yr), 1-sigma uncertainty = +/-"
                + "{:.2f}".format(segment.ss_rate_sig[i])
                + " (mm/yr)"
            )
        if segment.ds_rate_flag[i] == 1:
            logger.info(
                "Dip-slip rate constraint on "
                + segment.name[i].strip()
                + ": rate = "
                + "{:.2f}".format(segment.ds_rate[i])
                + " (mm/yr), 1-sigma uncertainty = +/-"
                + "{:.2f}".format(segment.ds_rate_sig[i])
                + " (mm/yr)"
            )
        if segment.ts_rate_flag[i] == 1:
            logger.info(
                "Tensile-slip rate constraint on "
                + segment.name[i].strip()
                + ": rate = "
                + "{:.2f}".format(segment.ts_rate[i])
                + " (mm/yr), 1-sigma uncertainty = +/-"
                + "{:.2f}".format(segment.ts_rate_sig[i])
                + " (mm/yr)"
            )

    slip_rate_constraint_partials = get_fault_slip_rate_partials(segment, block)
    slip_rate_constraint_flag = np.concatenate(
        (segment.ss_rate_flag, segment.ds_rate_flag, segment.ts_rate_flag)
    )
    assembly.index.slip_rate_constraints = np.where(slip_rate_constraint_flag == 1)[0]
    assembly.data.n_slip_rate_constraints = len(assembly.index.slip_rate_constraints)
    assembly.data.slip_rate_constraints = np.concatenate(
        (segment.ss_rate, segment.ds_rate, segment.ts_rate)
    )
    assembly.data.slip_rate_constraints = assembly.data.slip_rate_constraints[
        assembly.index.slip_rate_constraints
    ]
    assembly.sigma.slip_rate_constraints = np.concatenate(
        (segment.ss_rate_sig, segment.ds_rate_sig, segment.ts_rate_sig)
    )
    assembly.sigma.slip_rate_constraints = assembly.sigma.slip_rate_constraints[
        assembly.index.slip_rate_constraints
    ]
    slip_rate_constraint_partials = slip_rate_constraint_partials[
        assembly.index.slip_rate_constraints, :
    ]
    assembly.sigma.slip_rate_constraint_weight = command.slip_constraint_weight
    return assembly, slip_rate_constraint_partials


def get_segment_oblique_projection(lon1, lat1, lon2, lat2, skew=True):
    """
    Use pyproj oblique mercator: https://proj.org/operations/projections/omerc.html

    According to: https://proj.org/operations/projections/omerc.html
    This is this already rotated by the fault strike but the rotation can be undone with +no_rot
    > +no_rot
    > No rectification (not “no rotation” as one may well assume).
    > Do not take the last step from the skew uv-plane to the map XY plane.
    > Note: This option is probably only marginally useful,
    > but remains for (mostly) historical reasons.

    The version with north still pointing "up" appears to be called the
    Rectified skew orthomorphic projection or Hotine oblique Mercator projection
    https://pro.arcgis.com/en/pro-app/latest/help/mapping/properties/rectified-skew-orthomorphic.htm
    """
    if lon1 > 180.0:
        lon1 = lon1 - 360
    if lon2 > 180.0:
        lon2 = lon2 - 360
    projection_string = (
        "+proj=omerc "
        + "+lon_1="
        + str(lon1)
        + " "
        + "+lat_1="
        + str(lat1)
        + " "
        + "+lon_2="
        + str(lon2)
        + " "
        + "+lat_2="
        + str(lat2)
        + " "
        + "+ellps=WGS84"
    )
    if not skew:
        projection_string += " +no_rot"
    projection = pyproj.Proj(pyproj.CRS.from_proj4(projection_string))
    return projection


def get_okada_displacements(
    segment_lon1,
    segment_lat1,
    segment_lon2,
    segment_lat2,
    segment_locking_depth,
    segment_burial_depth,
    segment_dip,
    material_lambda,
    material_mu,
    strike_slip,
    dip_slip,
    tensile_slip,
    station_lon,
    station_lat,
):
    """
    Caculate elastic displacements in a homogeneous elastic half-space.
    Inputs are in geographic coordinates and then projected into a local
    xy-plane using a oblique Mercator projection that is tangent and parallel
    to the trace of the fault segment.  The elastic calculation is the
    original Okada 1992 Fortran code acceccesed through T. Ben Thompson's
    okada_wrapper: https://github.com/tbenthompson/okada_wrapper

    TODO: Locking depths are currently meters rather than KM in inputfiles!!!
    TODO: Is there another XYZ to ENU conversion needed?
    """
    segment_locking_depth *= celeri.KM2M
    segment_burial_depth *= celeri.KM2M

    # Project coordinates to flat space using a local oblique Mercator projection
    projection = celeri.get_segment_oblique_projection(
        segment_lon1, segment_lat1, segment_lon2, segment_lat2
    )
    station_x, station_y = projection(station_lon, station_lat)
    segment_x1, segment_y1 = projection(segment_lon1, segment_lat1)
    segment_x2, segment_y2 = projection(segment_lon2, segment_lat2)

    # Calculate geometric fault parameters
    segment_strike = np.arctan2(
        segment_y2 - segment_y1, segment_x2 - segment_x1
    )  # radians
    segment_length = np.sqrt(
        (segment_y2 - segment_y1) ** 2.0 + (segment_x2 - segment_x1) ** 2.0
    )
    segment_up_dip_width = (segment_locking_depth - segment_burial_depth) / np.sin(
        np.deg2rad(segment_dip)
    )

    # Translate stations and segment so that segment mid-point is at the origin
    segment_x_mid = (segment_x1 + segment_x2) / 2.0
    segment_y_mid = (segment_y1 + segment_y2) / 2.0
    station_x -= segment_x_mid
    station_y -= segment_y_mid
    segment_x1 -= segment_x_mid
    segment_x2 -= segment_x_mid
    segment_y1 -= segment_y_mid
    segment_y2 -= segment_y_mid

    # Unrotate coordinates to eliminate strike, segment will lie along y = 0
    rotation_matrix = np.array(
        [
            [np.cos(segment_strike), -np.sin(segment_strike)],
            [np.sin(segment_strike), np.cos(segment_strike)],
        ]
    )
    station_x_rotated, station_y_rotated = np.hsplit(
        np.einsum("ij,kj->ik", np.dstack((station_x, station_y))[0], rotation_matrix.T),
        2,
    )

    # Elastic displacements from Okada 1992
    alpha = (material_lambda + material_mu) / (material_lambda + 2 * material_mu)
    u_x = np.zeros_like(station_x)
    u_y = np.zeros_like(station_x)
    u_up = np.zeros_like(station_x)
    for i in range(len(station_x)):
        _, u, _ = okada_wrapper.dc3dwrapper(
            alpha,  # (lambda + mu) / (lambda + 2 * mu)
            [
                station_x_rotated[i],
                station_y_rotated[i],
                0,
            ],  # (meters) observation point
            segment_locking_depth,  # (meters) depth of the fault origin
            segment_dip,  # (degrees) the dip-angle of the rectangular dislocation surface
            [
                -segment_length / 2,
                segment_length / 2,
            ],  # (meters) the along-strike range of the surface (al1,al2 in the original)
            [
                0,
                segment_up_dip_width,
            ],  # (meters) along-dip range of the surface (aw1, aw2 in the original)
            [strike_slip, dip_slip, tensile_slip],
        )  # (meters) strike-slip, dip-slip, tensile-slip
        u_x[i] = u[0]
        u_y[i] = u[1]
        u_up[i] = u[2]

    # Un-rotate displacement to account for projected fault strike
    u_east, u_north = np.hsplit(
        np.einsum("ij,kj->ik", np.dstack((u_x, u_y))[0], rotation_matrix), 2
    )
    return u_east, u_north, u_up


def get_segment_station_operator_okada(segment, station, command):
    """
    Calculates the elastic displacement partial derivatives based on the Okada
    formulation, using the source and receiver geometries defined in
    dicitonaries segment and stations. Before calculating the partials for
    each segment, a local oblique Mercator project is done.

    The linear operator is structured as ():

                ss(segment1)  ds(segment1) ts(segment1) ... ss(segmentN) ds(segmentN) ts(segmentN)
    ve(station 1)
    vn(station 1)
    vu(station 1)
    .
    .
    .
    ve(station N)
    vn(station N)
    vu(station N)

    """
    if not station.empty:
        okada_segment_operator = np.ones((3 * len(station), 3 * len(segment)))
        # Loop through each segment and calculate displacements for each slip component
        for i in tqdm(
            range(len(segment)), desc="Calculating Okada partials for segments"
        ):
            (
                u_east_strike_slip,
                u_north_strike_slip,
                u_up_strike_slip,
            ) = celeri.get_okada_displacements(
                segment.lon1[i],
                segment.lat1[i],
                segment.lon2[i],
                segment.lat2[i],
                segment.locking_depth[i],
                segment.burial_depth[i],
                segment.dip[i],
                command.material_lambda,
                command.material_mu,
                1,
                0,
                0,
                station.lon,
                station.lat,
            )
            (
                u_east_dip_slip,
                u_north_dip_slip,
                u_up_dip_slip,
            ) = celeri.get_okada_displacements(
                segment.lon1[i],
                segment.lat1[i],
                segment.lon2[i],
                segment.lat2[i],
                segment.locking_depth[i],
                segment.burial_depth[i],
                segment.dip[i],
                command.material_lambda,
                command.material_mu,
                0,
                1,
                0,
                station.lon,
                station.lat,
            )
            (
                u_east_tensile_slip,
                u_north_tensile_slip,
                u_up_tensile_slip,
            ) = celeri.get_okada_displacements(
                segment.lon1[i],
                segment.lat1[i],
                segment.lon2[i],
                segment.lat2[i],
                segment.locking_depth[i],
                segment.burial_depth[i],
                segment.dip[i],
                command.material_lambda,
                command.material_mu,
                0,
                0,
                1,
                station.lon,
                station.lat,
            )
            segment_column_start_idx = 3 * i
            okada_segment_operator[0::3, segment_column_start_idx] = np.squeeze(
                u_east_strike_slip
            )
            okada_segment_operator[1::3, segment_column_start_idx] = np.squeeze(
                u_north_strike_slip
            )
            okada_segment_operator[2::3, segment_column_start_idx] = np.squeeze(
                u_up_strike_slip
            )
            okada_segment_operator[0::3, segment_column_start_idx + 1] = np.squeeze(
                u_east_dip_slip
            )
            okada_segment_operator[1::3, segment_column_start_idx + 1] = np.squeeze(
                u_north_dip_slip
            )
            okada_segment_operator[2::3, segment_column_start_idx + 1] = np.squeeze(
                u_up_dip_slip
            )
            okada_segment_operator[0::3, segment_column_start_idx + 2] = np.squeeze(
                u_east_tensile_slip
            )
            okada_segment_operator[1::3, segment_column_start_idx + 2] = np.squeeze(
                u_north_tensile_slip
            )
            okada_segment_operator[2::3, segment_column_start_idx + 2] = np.squeeze(
                u_up_tensile_slip
            )
    else:
        okada_segment_operator = np.empty(1)
    return okada_segment_operator


def station_row_keep(assembly):
    """
    Determines which station rows should be retained based on up velocities
    TODO: I do not understand this!!!
    TODO: The logic in the first conditional seems to indicate that if there are
    no vertical velocities as a part of the data then they should be eliminated.
    TODO: Perhaps it would be better to make this a flag in command???
    """
    if np.sum(np.abs(assembly.data.up_vel)) == 0:
        assembly.index.station_row_keep = np.setdiff1d(
            np.arange(0, assembly.index.sz_rotation[0]),
            np.arange(2, assembly.index.sz_rotation[0], 3),
        )
    else:
        assembly.index.station_row_keep = np.arange(0, assembly.index.sz_rotation[1])
    return assembly


def mogi_forward(mogi_lon, mogi_lat, mogi_depth, poissons_ratio, obs_lon, obs_lat):
    """
    Calculate displacements from a single Mogi source using
    equation 7.14 from "Earthquake and Volcano Deformation" by Paul Segall
    """
    u_east = np.zeros_like(obs_lon)
    u_north = np.zeros_like(obs_lon)
    u_up = np.zeros_like(obs_lon)
    for i in range(obs_lon.size):
        # Find angle between source and observation as well as distance betwen them
        source_to_obs_forward_azimuth, _, source_to_obs_distance = celeri.GEOID.inv(
            mogi_lon, mogi_lat, obs_lon[i], obs_lat[i]
        )

        # Mogi displacements in cylindrical coordinates
        u_up[i] = (
            (1 - poissons_ratio)
            / np.pi
            * mogi_depth
            / ((source_to_obs_distance ** 2.0 + mogi_depth ** 2) ** 1.5)
        )
        u_radial = (
            (1 - poissons_ratio)
            / np.pi
            * source_to_obs_distance
            / ((source_to_obs_distance ** 2 + mogi_depth ** 2.0) ** 1.5)
        )

        # Convert radial displacement to east and north components
        u_east[i] = u_radial * np.sin(np.deg2rad(source_to_obs_forward_azimuth))
        u_north[i] = u_radial * np.cos(np.deg2rad(source_to_obs_forward_azimuth))
    return u_east, u_north, u_up


def get_mogi_operator(mogi, station, command):
    """
    Mogi volume change to station displacment operator
    """
    if mogi.empty:
        mogi_operator = np.empty(0)
    else:
        poissons_ratio = command.material_mu / (
            2 * (command.material_lambda + command.material_mu)
        )
        mogi_operator = np.zeros((3 * len(station), len(mogi)))
        for i in range(len(mogi)):
            mogi_depth = celeri.KM2M * mogi.depth[i]
            u_east, u_north, u_up = mogi_forward(
                mogi.lon[i],
                mogi.lat[i],
                mogi_depth,
                poissons_ratio,
                station.lon,
                station.lat,
            )

            # Insert components into partials matrix
            mogi_operator[0::3, i] = u_east
            mogi_operator[1::3, i] = u_north
            mogi_operator[2::3, i] = u_up
    return mogi_operator


def interleave2(array_1, array_2):
    interleaved_array = np.empty((array_1.size + array_2.size), dtype=array_1.dtype)
    interleaved_array[0::2] = array_1
    interleaved_array[1::2] = array_2
    return interleaved_array


def interleave3(array_1, array_2, array_3):
    interleaved_array = np.empty(
        (array_1.size + array_2.size + array_3.size), dtype=array_1.dtype
    )
    interleaved_array[0::3] = array_1
    interleaved_array[1::3] = array_2
    interleaved_array[2::3] = array_3
    return interleaved_array


def latitude_to_colatitude(lat):
    """
    Convert from latitude to colatitude
    NOTE: Not sure why I need to treat the scalar case differently but I do.
    """
    if lat.size == 1:  # Deal with the scalar case
        if lat >= 0:
            lat = 90.0 - lat
        elif lat < 0:
            lat = -90.0 - lat
    else:  # Deal with the array case
        lat[np.where(lat >= 0)[0]] = 90.0 - lat[np.where(lat >= 0)[0]]
        lat[np.where(lat < 0)[0]] = -90.0 - lat[np.where(lat < 0)[0]]
    return lat


def get_block_centroid(segment, block_idx):
    """
    Calculate centroid of a block based on boundary polygon
    We take all block vertices (including duplicates) and estimate
    the centroid by taking the average of longitude and latitude
    weighted by the length of the segment that each vertex is
    attached to.
    """
    segments_with_block_idx = np.union1d(
        np.where(segment.west_labels == block_idx)[0],
        np.where(segment.east_labels == block_idx)[0],
    )
    lon0 = np.concatenate(
        (segment.lon1[segments_with_block_idx], segment.lon2[segments_with_block_idx])
    )
    lat0 = np.concatenate(
        (segment.lat1[segments_with_block_idx], segment.lat2[segments_with_block_idx])
    )
    lengths = np.concatenate(
        (
            segment.length[segments_with_block_idx],
            segment.length[segments_with_block_idx],
        )
    )
    block_centroid_lon = np.average(lon0, weights=lengths)
    block_centroid_lat = np.average(lat0, weights=lengths)
    return block_centroid_lon, block_centroid_lat


def get_strain_rate_displacements(
    lon_obs,
    lat_obs,
    centroid_lon,
    centroid_lat,
    strain_rate_lon_lon,
    strain_rate_lat_lat,
    strain_rate_lon_lat,
):
    """
    Calculate displacements due to three strain rate components.
    Equations are from Savage (2001) and expressed concisely in McCaffrey (2005)
    In McCaffrey (2005) these are the two unnumbered equations at the bottom
    of page 2
    """
    centroid_lon = np.deg2rad(centroid_lon)
    centroid_lat = celeri.latitude_to_colatitude(centroid_lat)
    centroid_lat = np.deg2rad(centroid_lat)
    lon_obs = np.deg2rad(lon_obs)
    lat_obs = celeri.latitude_to_colatitude(lat_obs)
    lat_obs = np.deg2rad(lat_obs)

    # Calculate displacements from homogeneous strain
    u_up = np.zeros(
        lon_obs.size
    )  # Always zero here because we're assuming plane strain on the sphere
    u_east = strain_rate_lon_lon * (
        celeri.RADIUS_EARTH * (lon_obs - centroid_lon) * np.sin(centroid_lat)
    ) + strain_rate_lon_lat * (celeri.RADIUS_EARTH * (lat_obs - centroid_lat))
    u_north = strain_rate_lon_lat * (
        celeri.RADIUS_EARTH * (lon_obs - centroid_lon) * np.sin(centroid_lat)
    ) + strain_rate_lat_lat * (celeri.RADIUS_EARTH * (lat_obs - centroid_lat))
    return u_east, u_north, u_up


def get_strain_rate_centroid_operator(block, station, segment):
    """
    Calculate strain partial derivatives assuming a strain centroid at the center of each block
    TODO: Return something related to assembly.index???
    """
    strain_rate_block_idx = np.where(block.strain_rate_flag.to_numpy() > 0)[0]
    if strain_rate_block_idx.size > 0:
        block_strain_rate_operator = np.zeros(
            (3 * len(station), 3 * strain_rate_block_idx.size)
        )
        for i in range(strain_rate_block_idx.size):
            # Find centroid of current block
            block_centroid_lon, block_centroid_lat = celeri.get_block_centroid(
                segment, strain_rate_block_idx[i]
            )

            # Find stations on current block
            station_idx = np.where(station.block_label == strain_rate_block_idx[i])[0]
            stations_block_lon = station.lon[station_idx].to_numpy()
            stations_block_lat = station.lat[station_idx].to_numpy()

            # Calculate partials for each component of strain rate
            (
                vel_east_lon_lon,
                vel_north_lon_lon,
                vel_up_lon_lon,
            ) = get_strain_rate_displacements(
                stations_block_lon,
                stations_block_lat,
                block_centroid_lon,
                block_centroid_lat,
                strain_rate_lon_lon=1,
                strain_rate_lat_lat=0,
                strain_rate_lon_lat=0,
            )
            (
                vel_east_lat_lat,
                vel_north_lat_lat,
                vel_up_lat_lat,
            ) = get_strain_rate_displacements(
                stations_block_lon,
                stations_block_lat,
                block_centroid_lon,
                block_centroid_lat,
                strain_rate_lon_lon=0,
                strain_rate_lat_lat=1,
                strain_rate_lon_lat=0,
            )
            (
                vel_east_lon_lat,
                vel_north_lon_lat,
                vel_up_lon_lat,
            ) = get_strain_rate_displacements(
                stations_block_lon,
                stations_block_lat,
                block_centroid_lon,
                block_centroid_lat,
                strain_rate_lon_lon=0,
                strain_rate_lat_lat=0,
                strain_rate_lon_lat=1,
            )
            block_strain_rate_operator[3 * station_idx, 3 * i] = vel_east_lon_lon
            block_strain_rate_operator[3 * station_idx, 3 * i + 1] = vel_east_lat_lat
            block_strain_rate_operator[3 * station_idx, 3 * i + 2] = vel_east_lon_lat
            block_strain_rate_operator[3 * station_idx + 1, 3 * i] = vel_north_lon_lon
            block_strain_rate_operator[
                3 * station_idx + 1, 3 * i + 1
            ] = vel_north_lat_lat
            block_strain_rate_operator[
                3 * station_idx + 1, 3 * i + 2
            ] = vel_north_lon_lat
            block_strain_rate_operator[3 * station_idx + 2, 3 * i] = vel_up_lon_lon
            block_strain_rate_operator[3 * station_idx + 2, 3 * i + 1] = vel_up_lat_lat
            block_strain_rate_operator[3 * station_idx + 2, 3 * i + 2] = vel_up_lon_lat
    else:
        block_strain_rate_operator = np.empty(0)
    return block_strain_rate_operator, strain_rate_block_idx


def get_rotation_displacements(lon_obs, lat_obs, omega_x, omega_y, omega_z):
    """
    Get displacments at at longitude and latitude coordinates given rotation
    vector components (omega_x, omega_y, omega_z)
    """
    vel_east = np.zeros(lon_obs.size)
    vel_north = np.zeros(lon_obs.size)
    vel_up = np.zeros(lon_obs.size)
    x, y, z = celeri.sph2cart(lon_obs, lat_obs, celeri.RADIUS_EARTH)
    for i in range(lon_obs.size):
        cross_product_operator = celeri.get_cross_partials([x[i], y[i], z[i]])
        (
            vel_north_from_omega_x,
            vel_east_from_omega_x,
            vel_up_from_omega_x,
        ) = celeri.cartesian_vector_to_spherical_vector(
            cross_product_operator[0, 0],
            cross_product_operator[1, 0],
            cross_product_operator[2, 0],
            lon_obs[i],
            lat_obs[i],
        )
        (
            vel_north_from_omega_y,
            vel_east_from_omega_y,
            vel_up_from_omega_y,
        ) = celeri.cartesian_vector_to_spherical_vector(
            cross_product_operator[0, 1],
            cross_product_operator[1, 1],
            cross_product_operator[2, 1],
            lon_obs[i],
            lat_obs[i],
        )
        (
            vel_north_from_omega_z,
            vel_east_from_omega_z,
            vel_up_from_omega_z,
        ) = celeri.cartesian_vector_to_spherical_vector(
            cross_product_operator[0, 2],
            cross_product_operator[1, 2],
            cross_product_operator[2, 2],
            lon_obs[i],
            lat_obs[i],
        )
        vel_east[i] = (
            omega_x * vel_east_from_omega_x
            + omega_y * vel_east_from_omega_y
            + omega_z * vel_east_from_omega_z
        )
        vel_north[i] = (
            omega_x * vel_north_from_omega_x
            + omega_y * vel_north_from_omega_y
            + omega_z * vel_north_from_omega_z
        )
    return vel_east, vel_north, vel_up


def get_block_rotation_operator(station):
    """
    Calculate block rotation partials operator for stations in dataframe
    station.
    """
    n_blocks = (
        np.max(station.block_label.values) + 1
    )  # +1 required so that a single block with index zero still propagates
    block_rotation_operator = np.zeros((3 * len(station), 3 * n_blocks))
    for i in range(n_blocks):
        station_idx = np.where(station.block_label == i)[0]
        (
            vel_east_omega_x,
            vel_north_omega_x,
            vel_up_omega_x,
        ) = get_rotation_displacements(
            station.lon.values[station_idx],
            station.lat.values[station_idx],
            omega_x=1,
            omega_y=0,
            omega_z=0,
        )
        (
            vel_east_omega_y,
            vel_north_omega_y,
            vel_up_omega_y,
        ) = get_rotation_displacements(
            station.lon.values[station_idx],
            station.lat.values[station_idx],
            omega_x=0,
            omega_y=1,
            omega_z=0,
        )
        (
            vel_east_omega_z,
            vel_north_omega_z,
            vel_up_omega_z,
        ) = get_rotation_displacements(
            station.lon.values[station_idx],
            station.lat.values[station_idx],
            omega_x=0,
            omega_y=0,
            omega_z=1,
        )
        block_rotation_operator[3 * station_idx, 3 * i] = vel_east_omega_x
        block_rotation_operator[3 * station_idx, 3 * i + 1] = vel_east_omega_y
        block_rotation_operator[3 * station_idx, 3 * i + 2] = vel_east_omega_z
        block_rotation_operator[3 * station_idx + 1, 3 * i] = vel_north_omega_x
        block_rotation_operator[3 * station_idx + 1, 3 * i + 1] = vel_north_omega_y
        block_rotation_operator[3 * station_idx + 1, 3 * i + 2] = vel_north_omega_z
        block_rotation_operator[3 * station_idx + 2, 3 * i] = vel_up_omega_x
        block_rotation_operator[3 * station_idx + 2, 3 * i + 1] = vel_up_omega_y
        block_rotation_operator[3 * station_idx + 2, 3 * i + 2] = vel_up_omega_z
    return block_rotation_operator


def get_global_float_block_rotation_operator(station):
    """
    Return a linear operator for the rotations of all stations assuming they
    are the on the same block (i.e., the globe). The purpose of this is to
    allow all of the stations to "float" in the inverse problem reducing the
    dependence on reference frame specification. This is done by making a
    copy of the station data frame, setting "block_label" for all stations
    equal to zero and then calling the standard block rotation operator
    function. The matrix returned here only has 3 columns
    """
    station_all_on_one_block = station.copy()
    station_all_on_one_block.block_label.values[
        :
    ] = 0  # Force all stations to be on one block
    global_float_block_rotation_operator = get_block_rotation_operator(
        station_all_on_one_block
    )
    return global_float_block_rotation_operator


def plot_block_labels(segment, block, station, closure):
    plt.figure()
    plt.title("West and east labels")
    for i in range(closure.n_polygons()):
        plt.plot(
            closure.polygons[i].vertices[:, 0],
            closure.polygons[i].vertices[:, 1],
            "k-",
            linewidth=0.5,
        )

    for i in range(len(segment)):
        plt.text(
            segment.mid_lon_plate_carree.values[i],
            segment.mid_lat_plate_carree.values[i],
            str(segment["west_labels"][i]) + "," + str(segment["east_labels"][i]),
            fontsize=8,
            color="m",
            horizontalalignment="center",
            verticalalignment="center",
        )

    for i in range(len(station)):
        plt.text(
            station.lon.values[i],
            station.lat.values[i],
            str(station.block_label[i]),
            fontsize=8,
            color="k",
            horizontalalignment="center",
            verticalalignment="center",
        )

    for i in range(len(block)):
        plt.text(
            block.interior_lon.values[i],
            block.interior_lat.values[i],
            str(block.block_label[i]),
            fontsize=8,
            color="g",
            horizontalalignment="center",
            verticalalignment="center",
        )

    plt.gca().set_aspect("equal")
    plt.show()


def get_transverse_projection(lon0, lat0):
    """
    Use pyproj oblique mercator: https://proj.org/operations/projections/tmerc.html
    """
    if lon0 > 180.0:
        lon0 = lon0 - 360
    projection_string = (
        "+proj=tmerc "
        + "+lon_0="
        + str(lon0)
        + " "
        + "+lat_0="
        + str(lat0)
        + " "
        + "+ellps=WGS84"
    )
    projection = pyproj.Proj(pyproj.CRS.from_proj4(projection_string))
    return projection


def get_tri_displacements(
    obs_lon,
    obs_lat,
    meshes,
    material_lambda,
    material_mu,
    tri_idx,
    strike_slip,
    dip_slip,
    tensile_slip,
):
    """
    Calculate surface displacments due to slip on a triangular dislocation
    element in a half space.  Includes projection from longitude and
    latitude to locally tangent planar coordinate system.
    """
    poissons_ratio = material_mu / (2 * (material_mu + material_lambda))

    # Project coordinates
    tri_centroid_lon = meshes[0].centroids[tri_idx, 0]
    tri_centroid_lat = meshes[0].centroids[tri_idx, 1]
    projection = get_transverse_projection(tri_centroid_lon, tri_centroid_lat)
    obs_x, obs_y = projection(obs_lon, obs_lat)
    tri_x1, tri_y1 = projection(meshes[0].lon1[tri_idx], meshes[0].lat1[tri_idx])
    tri_x2, tri_y2 = projection(meshes[0].lon2[tri_idx], meshes[0].lat2[tri_idx])
    tri_x3, tri_y3 = projection(meshes[0].lon3[tri_idx], meshes[0].lat3[tri_idx])
    tri_z1 = celeri.KM2M * meshes[0].dep1[tri_idx]
    tri_z2 = celeri.KM2M * meshes[0].dep2[tri_idx]
    tri_z3 = celeri.KM2M * meshes[0].dep3[tri_idx]

    # Package coordinates for cutde call
    obs_coords = np.vstack((obs_x, obs_y, np.zeros_like(obs_x))).T
    tri_coords = np.array(
        [[tri_x1, tri_y1, tri_z1], [tri_x2, tri_y2, tri_z2], [tri_x3, tri_y3, tri_z3]]
    )

    # Call cutde, multiply by displacements, and package for the return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        disp_mat = cutde_halfspace.disp_matrix(
            obs_pts=obs_coords, tris=np.array([tri_coords]), nu=poissons_ratio
        )
    slip = np.array([[strike_slip, dip_slip, tensile_slip]])
    disp = disp_mat.reshape((-1, 3)).dot(slip.flatten())
    vel_east = disp[0::3]
    vel_north = disp[1::3]
    vel_up = disp[2::3]
    return vel_east, vel_north, vel_up


def get_tde_velocities(meshes, station, command):
    """
    Calculates the elastic displacement partial derivatives based on the
    T. Ben Thomposen cutde of the Nikhool and Walters (2015) equations
    for the displacements resulting from slip on a triangular
    dislocation in a homogeneous elastic half space.

    The linear operator is structured as ():

                ss(tri1)  ds(tri1) ts(tri1) ... ss(triN) ds(triN) ts(triN)
    ve(station 1)
    vn(station 1)
    vu(station 1)
    .
    .
    .
    ve(station N)
    vn(station N)
    vu(station N)

    """
    if len(meshes) > 0:
        n_tris = meshes[0].lon1.size
        if not station.empty:
            tri_operator = np.zeros((3 * len(station), 3 * n_tris))

            # Loop through each segment and calculate displacements for each slip component
            for i in tqdm(
                range(n_tris), desc="Calculating cutde partials for triangles"
            ):
                # for i in tqdm(range(100), desc="Calculating cutde partials for triangles"):
                (
                    vel_east_strike_slip,
                    vel_north_strike_slip,
                    vel_up_strike_slip,
                ) = get_tri_displacements(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=1,
                    dip_slip=0,
                    tensile_slip=0,
                )
                (
                    vel_east_dip_slip,
                    vel_north_dip_slip,
                    vel_up_dip_slip,
                ) = get_tri_displacements(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=0,
                    dip_slip=1,
                    tensile_slip=0,
                )
                (
                    vel_east_tensile_slip,
                    vel_north_tensile_slip,
                    vel_up_tensile_slip,
                ) = get_tri_displacements(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=0,
                    dip_slip=0,
                    tensile_slip=1,
                )
                tri_operator[0::3, 3 * i] = np.squeeze(vel_east_strike_slip)
                tri_operator[1::3, 3 * i] = np.squeeze(vel_north_strike_slip)
                tri_operator[2::3, 3 * i] = np.squeeze(vel_up_strike_slip)
                tri_operator[0::3, 3 * i + 1] = np.squeeze(vel_east_dip_slip)
                tri_operator[1::3, 3 * i + 1] = np.squeeze(vel_north_dip_slip)
                tri_operator[2::3, 3 * i + 1] = np.squeeze(vel_up_dip_slip)
                tri_operator[0::3, 3 * i + 2] = np.squeeze(vel_east_tensile_slip)
                tri_operator[1::3, 3 * i + 2] = np.squeeze(vel_north_tensile_slip)
                tri_operator[2::3, 3 * i + 2] = np.squeeze(vel_up_tensile_slip)
        else:
            tri_operator = np.empty(0)
    else:
        tri_operator = np.empty(0)
    return tri_operator


def get_shared_sides(vertices):
    """
    Determine the indices of the triangular elements sharing
    one side with a particular element.
    Inputs:
    vertices: n x 3 array containing the 3 vertex indices of the n elements,
        assumes that values increase monotonically from 1:n

    Outputs:
    share: n x 3 array containing the indices of the m elements sharing a
        side with each of the n elements.  "-1" values in the array
        indicate elements with fewer than m neighbors (i.e., on
        the edge of the geometry).

    In general, elements will have 1 (mesh corners), 2 (mesh edges), or 3
    (mesh interiors) neighbors, but in the case of branching faults that
    have been adjusted with mergepatches, it's for edges and corners to
    also up to 3 neighbors.
    """
    # Make side arrays containing vertex indices of sides
    side_1 = np.sort(np.vstack((vertices[:, 0], vertices[:, 1])).T, 1)
    side_2 = np.sort(np.vstack((vertices[:, 1], vertices[:, 2])).T, 1)
    side_3 = np.sort(np.vstack((vertices[:, 0], vertices[:, 2])).T, 1)
    sides_all = np.vstack((side_1, side_2, side_3))

    # Find the unique sides - each side can part of at most 2 elements
    _, first_occurence_idx = np.unique(sides_all, return_index=True, axis=0)
    _, last_occurence_idx = np.unique(np.flipud(sides_all), return_index=True, axis=0)
    last_occurence_idx = sides_all.shape[0] - last_occurence_idx - 1

    # Shared sides are those whose first and last indices are not equal
    shared = np.where((last_occurence_idx - first_occurence_idx) != 0)[0]

    # These are the indices of the shared sides
    sside1 = first_occurence_idx[shared]  # What should I name these variables?
    sside2 = last_occurence_idx[shared]

    el1, sh1 = np.unravel_index(
        sside1, vertices.shape, order="F"
    )  # "F" is for fortran ordering.  What should I call this variables?
    el2, sh2 = np.unravel_index(sside2, vertices.shape, order="F")
    share = -1 * np.ones((vertices.shape[0], 3))
    for i in range(el1.size):
        share[el1[i], sh1[i]] = el2[i]
        share[el2[i], sh2[i]] = el1[i]
    share = share.astype(int)
    return share


def get_tri_shared_sides_distances(share, x_centroid, y_centroid, z_centroid):
    """
    Calculates the distances between the centroids of adjacent triangular
    elements, for use in smoothing algorithms.

    Inputs:
    share: n x 3 array output from ShareSides, containing the indices
        of up to 3 elements that share a side with each of the n elements.
    x_centroid: x coordinates of element centroids
    y_centroid: y coordinates of element centroids
    z_centroid: z coordinates of element centroids

    Outputs:
    dists: n x 3 array containing distance between each of the n elements
        and its 3 or fewer neighbors.  A distance of 0 does not imply
        collocated elements, but rather implies that there are fewer
        than 3 elements that share a side with the element in that row.
    """
    tri_shared_sides_distances = np.zeros(share.shape)
    for i in range(share.shape[0]):
        tri_shared_sides_distances[i, :] = np.sqrt(
            (x_centroid[i] - x_centroid[share[i, :]]) ** 2.0
            + (y_centroid[i] - y_centroid[share[i, :]]) ** 2.0
            + (z_centroid[i] - z_centroid[share[i, :]]) ** 2.0
        )
    tri_shared_sides_distances[np.where(share == -1)] = 0
    return tri_shared_sides_distances


def get_tri_smoothing_matrix(share, tri_shared_sides_distances):
    """
    Produces a smoothing matrix based on the scale-dependent
    umbrella operator (e.g., Desbrun et al., 1999; Resor, 2004).

    Inputs:
    share: n x 3 array of indices of the up to 3 elements sharing a side
        with each of the n elements
    tri_shared_sides_distances: n x 3 array of distances between each of the
        n elements and its up to 3 neighbors

    Outputs:
    smoothing matrix: 3n x 3n smoothing matrix
    """

    # Allocate sparse matrix for contructing smoothing matrix
    n_shared_tris = share.shape[0]
    smoothing_matrix = scipy.sparse.lil_matrix((3 * n_shared_tris, 3 * n_shared_tris))

    # Create a design matrix for Laplacian construction
    share_copy = celeri.copy.deepcopy(share)
    share_copy[np.where(share == -1)] = 0
    share_copy[np.where(share != -1)] = 1

    # Sum the distances between each element and its neighbors
    share_distances = np.sum(tri_shared_sides_distances, axis=1)
    leading_coefficient = 2.0 / share_distances

    # Replace zero distances with 1 to avoid divide by zero.  TODO: should this be capped at a
    tri_shared_sides_distances[np.where(tri_shared_sides_distances == 0)] = 1

    # Take the reciprocal of the distances
    inverse_tri_shared_sides_distances = 1.0 / tri_shared_sides_distances

    # Diagonal terms # TODO: Defnitely not sure about his line!!!
    diagonal_terms = -leading_coefficient * np.sum(
        inverse_tri_shared_sides_distances * share_copy,
        axis=1,
    )

    # Off-diagonal terms
    off_diagonal_terms = (
        np.vstack((leading_coefficient, leading_coefficient, leading_coefficient)).T
        * inverse_tri_shared_sides_distances
        * share_copy
    )

    # Place the weights into the smoothing operator
    for j in range(3):
        for i in range(n_shared_tris):
            smoothing_matrix[3 * i + j, 3 * i + j] = diagonal_terms[i]
            if share[i, j] != -1:
                k = 3 * i + np.array([0, 1, 2])
                m = 3 * share[i, j] + np.array([0, 1, 2])
                smoothing_matrix[k, m] = off_diagonal_terms[i, j]
    return smoothing_matrix


def get_all_mesh_smoothing_matrices(meshes: List, operators: Dict):
    """
    Build smoothing matrices for each of the triangular meshes
    stored in meshes
    """
    for i in range(len(meshes)):
        # Get smoothing operator for a single mesh.
        meshes[i].share = celeri.get_shared_sides(meshes[i].verts)
        meshes[i].tri_shared_sides_distances = celeri.get_tri_shared_sides_distances(
            meshes[i].share,
            meshes[i].x_centroid,
            meshes[i].y_centroid,
            meshes[i].z_centroid,
        )
        operators.meshes[i].smoothing_matrix = celeri.get_tri_smoothing_matrix(
            meshes[i].share, meshes[i].tri_shared_sides_distances
        )


def get_all_mesh_smoothing_matrices_simple(meshes: List, operators: Dict):
    """
    Build smoothing matrices for each of the triangular meshes
    stored in meshes
    These are the simple not distance weighted meshes
    """
    for i in range(len(meshes)):
        # Get smoothing operator for a single mesh.
        meshes[i].share = celeri.get_shared_sides(meshes[i].verts)
        meshes[i].tri_shared_sides_distances = celeri.get_tri_shared_sides_distances(
            meshes[i].share,
            meshes[i].x_centroid,
            meshes[i].y_centroid,
            meshes[i].z_centroid,
        )
        operators.meshes[
            i
        ].smoothing_matrix_simple = celeri.get_tri_smoothing_matrix_simple(
            meshes[i].share, N_MESH_DIM
        )


def get_tri_smoothing_matrix_simple(share, n_dim):
    """
    Produces a smoothing matrix based without scale-dependent
    weighting.

    Inputs:
    share: n x 3 array of indices of the up to 3 elements sharing a side
        with each of the n elements

    Outputs:
    smoothing matrix: n_dim * n x n_dim * n smoothing matrix
    """

    # Allocate sparse matrix for contructing smoothing matrix
    n_shared_tri = share.shape[0]
    smoothing_matrix = scipy.sparse.lil_matrix(
        (n_dim * n_shared_tri, n_dim * n_shared_tri)
    )

    for j in range(n_dim):
        for i in range(n_shared_tri):
            smoothing_matrix[n_dim * i + j, n_dim * i + j] = 3
            if share[i, j] != -1:
                k = n_dim * i + np.arange(n_dim)
                m = n_dim * share[i, j] + np.arange(n_dim)
                smoothing_matrix[k, m] = -1
    return smoothing_matrix


def get_ordered_edge_nodes(meshes: List) -> None:
    """Find exterior edges of each mesh and return them in the dictionary
    for each mesh.

    Args:
        meshes (List): list of mesh dictionaries
    """

    for i in range(len(meshes)):
        # Make side arrays containing vertex indices of sides
        vertices = meshes[i].verts
        side_1 = np.sort(np.vstack((vertices[:, 0], vertices[:, 1])).T, 1)
        side_2 = np.sort(np.vstack((vertices[:, 1], vertices[:, 2])).T, 1)
        side_3 = np.sort(np.vstack((vertices[:, 2], vertices[:, 0])).T, 1)
        all_sides = np.vstack((side_1, side_2, side_3))
        unique_sides, sides_count = np.unique(all_sides, return_counts=True, axis=0)
        edge_nodes = unique_sides[np.where(sides_count == 1)]

        meshes[i].ordered_edge_nodes = np.zeros_like(edge_nodes)
        meshes[i].ordered_edge_nodes[0, :] = edge_nodes[0, :]
        last_row = 0
        for j in range(1, len(edge_nodes)):
            idx = np.where(
                (edge_nodes == meshes[i].ordered_edge_nodes[j - 1, 1])
            )  # Edge node indices the same as previous row, second column
            next_idx = np.where(
                idx[0][:] != last_row
            )  # One of those indices is the last row itself. Find the other row index
            next_row = idx[0][next_idx]  # Index of the next ordered row
            next_col = idx[1][next_idx]  # Index of the next ordered column (1 or 2)
            if next_col == 1:
                next_col_ord = [1, 0]  # Flip edge ordering
            else:
                next_col_ord = [0, 1]
            meshes[i].ordered_edge_nodes[j, :] = edge_nodes[next_row, next_col_ord]
            last_row = (
                next_row  # Update last_row so that it's excluded in the next iteration
            )


def plot_segment_displacements(
    segment,
    station,
    command,
    segment_idx,
    strike_slip,
    dip_slip,
    tensile_slip,
    lon_min,
    lon_max,
    lat_min,
    lat_max,
    quiver_scale,
):
    u_east, u_north, u_up = celeri.get_okada_displacements(
        segment.lon1.values[segment_idx],
        segment.lat1[segment_idx],
        segment.lon2[segment_idx],
        segment.lat2[segment_idx],
        segment.locking_depth[segment_idx],
        segment.burial_depth[segment_idx],
        segment.dip[segment_idx],
        command.material_lambda,
        command.material_mu,
        strike_slip,
        dip_slip,
        tensile_slip,
        station.lon,
        station.lat,
    )
    plt.figure()
    plt.plot(
        [segment.lon1[segment_idx], segment.lon2[segment_idx]],
        [segment.lat1[segment_idx], segment.lat2[segment_idx]],
        "-r",
    )
    plt.quiver(
        station.lon,
        station.lat,
        u_east,
        u_north,
        scale=quiver_scale,
        scale_units="inches",
    )
    plt.xlim([lon_min, lon_max])
    plt.ylim([lat_min, lat_max])
    plt.gca().set_aspect("equal", adjustable="box")
    plt.title("Okada displacements: longitude and latitude")
    plt.show()


def plot_strain_rate_components_for_block(closure, segment, station, block_idx):
    plt.figure(figsize=(10, 3))
    plt.subplot(1, 3, 1)
    vel_east, vel_north, vel_up = get_strain_rate_displacements(
        station,
        segment,
        block_idx=block_idx,
        strain_rate_lon_lon=1,
        strain_rate_lat_lat=0,
        strain_rate_lon_lat=0,
    )
    for i in range(closure.n_polygons()):
        plt.plot(
            closure.polygons[i].vertices[:, 0],
            closure.polygons[i].vertices[:, 1],
            "k-",
            linewidth=0.5,
        )
    plt.quiver(
        station.lon,
        station.lat,
        vel_east,
        vel_north,
        scale=1e7,
        scale_units="inches",
        color="r",
    )

    plt.subplot(1, 3, 2)
    vel_east, vel_north, vel_up = get_strain_rate_displacements(
        station,
        segment,
        block_idx=block_idx,
        strain_rate_lon_lon=0,
        strain_rate_lat_lat=1,
        strain_rate_lon_lat=0,
    )
    for i in range(closure.n_polygons()):
        plt.plot(
            closure.polygons[i].vertices[:, 0],
            closure.polygons[i].vertices[:, 1],
            "k-",
            linewidth=0.5,
        )
    plt.quiver(
        station.lon,
        station.lat,
        vel_east,
        vel_north,
        scale=1e7,
        scale_units="inches",
        color="r",
    )

    plt.subplot(1, 3, 3)
    vel_east, vel_north, vel_up = get_strain_rate_displacements(
        station,
        segment,
        block_idx=block_idx,
        strain_rate_lon_lon=0,
        strain_rate_lat_lat=0,
        strain_rate_lon_lat=1,
    )
    for i in range(closure.n_polygons()):
        plt.plot(
            closure.polygons[i].vertices[:, 0],
            closure.polygons[i].vertices[:, 1],
            "k-",
            linewidth=0.5,
        )
    plt.quiver(
        station.lon,
        station.lat,
        vel_east,
        vel_north,
        scale=1e7,
        scale_units="inches",
        color="r",
    )
    plt.show()


def plot_rotation_components(closure, station):
    plt.figure(figsize=(10, 3))
    plt.subplot(1, 3, 1)
    vel_east, vel_north, vel_up = get_rotation_displacements(
        station.lon.values,
        station.lat.values,
        omega_x=1,
        omega_y=0,
        omega_z=0,
    )
    for i in range(closure.n_polygons()):
        plt.plot(
            closure.polygons[i].vertices[:, 0],
            closure.polygons[i].vertices[:, 1],
            "k-",
            linewidth=0.5,
        )
    plt.quiver(
        station.lon,
        station.lat,
        vel_east,
        vel_north,
        scale=1e7,
        scale_units="inches",
        color="r",
    )

    plt.subplot(1, 3, 2)
    vel_east, vel_north, vel_up = get_rotation_displacements(
        station.lon.values,
        station.lat.values,
        omega_x=0,
        omega_y=1,
        omega_z=0,
    )
    for i in range(closure.n_polygons()):
        plt.plot(
            closure.polygons[i].vertices[:, 0],
            closure.polygons[i].vertices[:, 1],
            "k-",
            linewidth=0.5,
        )
    plt.quiver(
        station.lon,
        station.lat,
        vel_east,
        vel_north,
        scale=1e7,
        scale_units="inches",
        color="r",
    )

    plt.subplot(1, 3, 3)
    vel_east, vel_north, vel_up = get_rotation_displacements(
        station.lon.values,
        station.lat.values,
        omega_x=0,
        omega_y=0,
        omega_z=1,
    )
    for i in range(closure.n_polygons()):
        plt.plot(
            closure.polygons[i].vertices[:, 0],
            closure.polygons[i].vertices[:, 1],
            "k-",
            linewidth=0.5,
        )
    plt.quiver(
        station.lon,
        station.lat,
        vel_east,
        vel_north,
        scale=1e7,
        scale_units="inches",
        color="r",
    )
    plt.show()


def plot_estimation_summary(
    segment: pd.DataFrame,
    station: pd.DataFrame,
    estimation: Dict,
    lon_range: Tuple,
    lat_range: Tuple,
    quiver_scale: float,
):
    """Plot overview figures showing observed and modeled velocities as well
    as velocity decomposition and estimates slip rates.

    Args:
        segment (pd.DataFrame): Fault segments
        station (pd.DataFrame): GPS observations
        estimation (Dict): All estimated values
        lon_range (Tuple): Latitude range (min, max)
        lat_range (Tuple): Latitude range (min, max)
        quiver_scale (float): Scaling for velocity arrows
    """

    def common_plot_elements(segment: pd.DataFrame, lon_range: Tuple, lat_range: Tuple):
        """Elements common to all subplots

        Args:
            segment (pd.DataFrame): Fault segments
            lon_range (Tuple): Longitude range (min, max)
            lat_range (Tuple): Latitude range (min, max)
        """
        for i in range(len(segment)):
            plt.plot(
                [segment.lon1[i], segment.lon2[i]],
                [segment.lat1[i], segment.lat2[i]],
                "-k",
                linewidth=0.5,
            )
        plt.xlim([lon_range[0], lon_range[1]])
        plt.ylim([lat_range[0], lat_range[1]])
        plt.gca().set_aspect("equal", adjustable="box")

    max_sigma_cutoff = 99.0
    n_subplot_rows = 3
    n_subplot_cols = 3
    plt.figure(figsize=(12, 14))
    ax1 = plt.subplot(n_subplot_rows, n_subplot_cols, 1)
    plt.title("observed velocities")
    common_plot_elements(segment, lon_range, lat_range)
    plt.quiver(
        station.lon,
        station.lat,
        station.east_vel,
        station.north_vel,
        scale=quiver_scale,
        scale_units="inches",
        color="red",
    )

    plt.subplot(n_subplot_rows, n_subplot_cols, 2, sharex=ax1, sharey=ax1)
    plt.title("model velocities")
    common_plot_elements(segment, lon_range, lat_range)
    plt.quiver(
        station.lon,
        station.lat,
        estimation.east_vel,
        estimation.north_vel,
        scale=quiver_scale,
        scale_units="inches",
        color="blue",
    )

    plt.subplot(n_subplot_rows, n_subplot_cols, 3, sharex=ax1, sharey=ax1)
    plt.title("residual velocities")
    common_plot_elements(segment, lon_range, lat_range)
    plt.quiver(
        station.lon,
        station.lat,
        estimation.east_vel_residual,
        estimation.north_vel_residual,
        scale=quiver_scale,
        scale_units="inches",
        color="green",
    )

    plt.subplot(n_subplot_rows, n_subplot_cols, 4, sharex=ax1, sharey=ax1)
    plt.title("rotation velocities")
    common_plot_elements(segment, lon_range, lat_range)
    plt.quiver(
        station.lon,
        station.lat,
        estimation.east_vel_rotation,
        estimation.north_vel_rotation,
        scale=quiver_scale,
        scale_units="inches",
        color="orange",
    )

    plt.subplot(n_subplot_rows, n_subplot_cols, 5, sharex=ax1, sharey=ax1)
    plt.title("elastic segment velocities")
    common_plot_elements(segment, lon_range, lat_range)
    plt.quiver(
        station.lon,
        station.lat,
        estimation.east_vel_elastic_segment,
        estimation.north_vel_elastic_segment,
        scale=quiver_scale,
        scale_units="inches",
        color="magenta",
    )

    plt.subplot(n_subplot_rows, n_subplot_cols, 6, sharex=ax1, sharey=ax1)
    plt.title("elastic tde velocities")
    common_plot_elements(segment, lon_range, lat_range)
    plt.quiver(
        station.lon,
        station.lat,
        estimation.east_vel_tde,
        estimation.north_vel_tde,
        scale=quiver_scale,
        scale_units="inches",
        color="black",
    )

    plt.subplot(n_subplot_rows, n_subplot_cols, 7, sharex=ax1, sharey=ax1)
    plt.title("segment strike-slip (negative right-lateral)")
    common_plot_elements(segment, lon_range, lat_range)
    for i in range(len(segment)):
        if estimation.strike_slip_rate_sigma[i] < max_sigma_cutoff:
            plt.text(
                segment.mid_lon_plate_carree[i],
                segment.mid_lat_plate_carree[i],
                f"{estimation.strike_slip_rates[i]:.1f}({estimation.strike_slip_rate_sigma[i]:.1f})",
                color="red",
                clip_on=True,
                horizontalalignment="center",
                verticalalignment="center",
                fontsize=7,
            )
        else:
            plt.text(
                segment.mid_lon_plate_carree[i],
                segment.mid_lat_plate_carree[i],
                f"{estimation.strike_slip_rates[i]:.1f}(*)",
                color="red",
                clip_on=True,
                horizontalalignment="center",
                verticalalignment="center",
                fontsize=7,
            )

    plt.subplot(n_subplot_rows, n_subplot_cols, 8, sharex=ax1, sharey=ax1)
    plt.title("segment dip-slip (positive convergences)")
    common_plot_elements(segment, lon_range, lat_range)
    for i in range(len(segment)):
        if estimation.dip_slip_rate_sigma[i] < max_sigma_cutoff:
            plt.text(
                segment.mid_lon_plate_carree[i],
                segment.mid_lat_plate_carree[i],
                f"{estimation.dip_slip_rates[i]:.1f}({estimation.dip_slip_rate_sigma[i]:.1f})",
                color="blue",
                clip_on=True,
                horizontalalignment="center",
                verticalalignment="center",
                fontsize=7,
            )
        else:
            plt.text(
                segment.mid_lon_plate_carree[i],
                segment.mid_lat_plate_carree[i],
                f"{estimation.dip_slip_rates[i]:.1f}(*)",
                color="blue",
                clip_on=True,
                horizontalalignment="center",
                verticalalignment="center",
                fontsize=7,
            )

    plt.subplot(n_subplot_rows, n_subplot_cols, 9, sharex=ax1, sharey=ax1)
    plt.title("segment tensile-slip (negative convergences)")
    common_plot_elements(segment, lon_range, lat_range)
    for i in range(len(segment)):
        if estimation.tensile_slip_rate_sigma[i] < max_sigma_cutoff:
            plt.text(
                segment.mid_lon_plate_carree[i],
                segment.mid_lat_plate_carree[i],
                f"{estimation.tensile_slip_rates[i]:.1f}({estimation.tensile_slip_rate_sigma[i]:.1f})",
                color="green",
                clip_on=True,
                horizontalalignment="center",
                verticalalignment="center",
                fontsize=7,
            )
        else:
            plt.text(
                segment.mid_lon_plate_carree[i],
                segment.mid_lat_plate_carree[i],
                f"{estimation.tensile_slip_rates[i]:.1f}(*)",
                color="green",
                clip_on=True,
                horizontalalignment="center",
                verticalalignment="center",
                fontsize=7,
            )
    plt.show()


def test_end2end():
    """
    This doesn't actually check for correctness much at all,
    but just tests to make sure that a full block model run executes without errors.
    """
    command_file_name = "./data/western_north_america/basic_command.json"
    command, segment, block, meshes, station, mogi, sar = celeri.read_data(
        command_file_name
    )
    station = celeri.process_station(station, command)
    segment = celeri.process_segment(segment, command, meshes)
    sar = celeri.process_sar(sar, command)
    closure, block = celeri.assign_block_labels(segment, station, block, mogi, sar)
    assert closure.n_polygons() == 31

    assembly = addict.Dict()
    operators = addict.Dict()
    assembly = celeri.merge_geodetic_data(assembly, station, sar)
    assembly, operators.block_motion_constraints = celeri.block_constraints(
        assembly, block, command
    )
    assembly, operators.slip_rate_constraints = celeri.slip_rate_constraints(
        assembly, segment, block, command
    )
    # TODO: Compute TDE partials: check with npy file like block closure


def test_global_closure():
    """
    This check to make sure that the closure algorithm returns a known
    (and hopefully correct!) answer for the global closure problem.
    Right now all this does is check for the correct number of blocks and
    against one set of polygon edge indices
    """
    command_file_name = "./data/global/global_command.json"
    command, segment, block, meshes, station, mogi, sar = celeri.read_data(
        command_file_name
    )
    station = celeri.process_station(station, command)
    segment = celeri.process_segment(segment, command, meshes)
    sar = celeri.process_sar(sar, command)
    closure, block = celeri.assign_block_labels(segment, station, block, mogi, sar)

    # Compare calculated edge indices with stored edge indices
    all_edge_idxs = np.array([])
    for i in range(closure.n_polygons()):
        all_edge_idxs = np.concatenate(
            (all_edge_idxs, np.array(closure.polygons[i].edge_idxs))
        )

    with open("global_closure_test_data.npy", "rb") as f:
        all_edge_idxs_stored = np.load(f)

    assert np.allclose(all_edge_idxs, all_edge_idxs_stored)


def test_okada_equals_cutde():
    # Observation coordinates
    x_obs = np.array([2.0])
    y_obs = np.array([1.0])
    z_obs = np.array([0.0])

    nx = ny = 100
    x_obs_vec = np.linspace(-1.0, 1.0, nx)
    y_obs_vec = np.linspace(-1.0, 1.0, ny)

    x_obs_mat, y_obs_mat = np.meshgrid(x_obs_vec, y_obs_vec)
    x_obs = x_obs_mat.flatten()
    y_obs = y_obs_mat.flatten()
    z_obs = np.zeros_like(x_obs)

    # Storage for displacements
    u_x_okada = np.zeros_like(x_obs)
    u_y_okada = np.zeros_like(y_obs)
    u_z_okada = np.zeros_like(z_obs)

    # Material properties
    material_lambda = 3e10
    material_mu = 3e10
    poissons_ratio = material_lambda / (2 * (material_lambda + material_mu))
    alpha = (material_lambda + material_mu) / (material_lambda + 2 * material_mu)

    # Fault slip
    strike_slip = 1
    dip_slip = 0
    tensile_slip = 0

    # Parameters for Okada
    segment_locking_depth = 1.0
    segment_dip = 90
    segment_length = 1.0
    segment_up_dip_width = segment_locking_depth

    # Okada
    for i in range(x_obs.size):
        _, u, _ = okada_wrapper.dc3dwrapper(
            alpha,  # (lambda + mu) / (lambda + 2 * mu)
            [
                x_obs[i],
                y_obs[i],
                z_obs[i],
            ],  # (meters) observation point
            segment_locking_depth,  # (meters) depth of the fault origin
            segment_dip,  # (degrees) the dip-angle of the rectangular dislocation surface
            [
                -segment_length / 2,
                segment_length / 2,
            ],  # (meters) the along-strike range of the surface (al1,al2 in the original)
            [
                0,
                segment_up_dip_width,
            ],  # (meters) along-dip range of the surface (aw1, aw2 in the original)
            [strike_slip, dip_slip, tensile_slip],
        )  # (meters) strike-slip, dip-slip, tensile-slip
        u_x_okada[i] = u[0]
        u_y_okada[i] = u[1]
        u_z_okada[i] = u[2]

    # cutde
    tri_x1 = np.array([-0.5, -0.5])
    tri_y1 = np.array([0, 0])
    tri_z1 = np.array([0, 0])
    tri_x2 = np.array([0.5, 0.5])
    tri_y2 = np.array([0, 0.0])
    tri_z2 = np.array([0, -1])
    tri_x3 = np.array([0.5, -0.5])
    tri_y3 = np.array([0, 0])
    tri_z3 = np.array([-1, -1])

    # Package coordinates for cutde call
    obs_coords = np.vstack((x_obs, y_obs, np.zeros_like(x_obs))).T
    tri_coords = np.array(
        [[tri_x1, tri_y1, tri_z1], [tri_x2, tri_y2, tri_z2], [tri_x3, tri_y3, tri_z3]]
    ).astype(float)
    tri_coords = np.transpose(tri_coords, (2, 0, 1))

    # Call cutde, multiply by displacements, and package for the return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        disp_mat = cutde_halfspace.disp_matrix(
            obs_pts=obs_coords, tris=tri_coords, nu=poissons_ratio
        )
    slip = np.array([[strike_slip, dip_slip, tensile_slip]])

    # What to do with array dimensions here? We want to compute the
    # displacements at the observation points due to the slip specified
    # in the slip array. That will involve a dot product of the disp_mat with slip.
    # But, it's not that simple. First, take a look at the array shapes:
    # disp_mat has shape (10000, 3, 2, 3)
    # Why?
    # dimension #1: 10000 is the number of observation points
    # dimension #2: 3 is the number of components of the displacement vector
    # dimension #3: 2 is the number of source triangles
    # dimension #3: 3 is the number of components of the slip vector.
    #
    # Then, slip has shape (1, 3)
    # This is sort of "wrong" in that the first dimension should be the number of triangles.
    # But, since we're applying the same slip to both triangles, it's okay.
    #
    # So, to apply that slip to both triangles, we want to first do a do dot product
    # between disp_mat and slip[0] which will multiply and sum the last axis of both arrays
    #
    # Then, we will have a (10000, 3, 2) shape array.
    # Next, since we want the displacement due to the sum of both arrays, let's sum over that
    # last axis with two elements.
    #
    # The final result "u_cutde" will be a (10000, 3) array with the components of displacement
    # for every observation point.
    u_cutde = np.sum(disp_mat.dot(slip[0]), axis=2)

    # Uncomment to plot.
    # field_cutde = u_cutde[:,0].reshape((nx, ny))
    # field_okada = u_x_okada.reshape((nx, ny))
    # plt.subplot(1,2,1)
    # plt.contourf(x_obs_mat, y_obs_mat, field_cutde)
    # plt.colorbar()
    # plt.subplot(1,2,2)
    # plt.contourf(x_obs_mat, y_obs_mat, field_okada)
    # plt.colorbar()
    # plt.show()

    np.testing.assert_almost_equal(u_cutde[:, 0], u_x_okada)
    np.testing.assert_almost_equal(u_cutde[:, 1], u_y_okada)
    np.testing.assert_almost_equal(u_cutde[:, 2], u_z_okada)
