#!/usr/bin/env python
# Filename: dem_headwall_extraction 
"""
introduction: extraction RTS headwall from DEM

authors: Huang Lingcao
email:huanglingcao@gmail.com
add time: 30 April, 2021
"""

import os,sys
from optparse import OptionParser
import time

deeplabforRS =  os.path.expanduser('~/codes/PycharmProjects/DeeplabforRS')
sys.path.insert(0, deeplabforRS)

import vector_gpd

import basic_src.io_function as io_function
import basic_src.basic as basic
import basic_src.map_projection as map_projection
import raster_io

import cv2
import numpy as np
import geopandas as gpd
import pandas as pd

from dem_common import dem_headwall_shp_dir

def slope_tif_to_slope_shapefile(slope_tif,slope_bin_path,slope_threshold):

    if os.path.isfile(slope_bin_path):
        print('%s exist'%slope_bin_path)
    else:
        slope_data, nodata = raster_io.read_raster_one_band_np(slope_tif)
        bin_slope = np.zeros_like(slope_data,dtype=np.uint8)
        bin_slope[slope_data > slope_threshold] = 1
        bin_slope[slope_data > 88] = 0          # if slope is too large, it may caused by artifacts, so remove them

        # # Dilation or opening
        # # https://opencv-python-tutroals.readthedocs.io/en/latest/py_tutorials/py_imgproc/py_morphological_ops/py_morphological_ops.html
        # kernel = np.ones((3, 3), np.uint8)  # if kernal is 5 or larger, will remove some narrow parts.
        # # bin_slope = cv2.dilate(bin_slope,kernel,iterations = 1)
        # bin_slope = cv2.morphologyEx(bin_slope, cv2.MORPH_OPEN, kernel)     # use opening to remove some noise
        # # bin_slope = cv2.morphologyEx(bin_slope, cv2.MORPH_CLOSE, kernel)    # closing small holes inside

        # save
        slope_bin = bin_slope*255
        raster_io.save_numpy_array_to_rasterfile(slope_bin,slope_bin_path,slope_tif,nodata=0,compress='lzw',tiled='yes',bigtiff='if_safer')   # set nodata as 0

    # to shapefile
    slope_bin_shp = vector_gpd.raster2shapefile(slope_bin_path,connect8=True)
    if slope_bin_shp is None:
        return False
    return slope_bin_shp

def remove_based_on_area(slope_bin_shp,min_area, max_area,wkt, rm_area_shp):
    polygons = vector_gpd.read_polygons_gpd(slope_bin_shp,b_fix_invalid_polygon=False)

    remain_polygons = []
    # remove relative large but narrow ones.
    remove_count = 0
    for idx, poly in enumerate(polygons):
        # remove quite large or too small ones
        if poly.area > max_area or poly.area < min_area:
            remove_count += 1
            continue
        remain_polygons.append(poly)

    basic.outputlogMessage('remove %d polygons based on area, remain %d ones saving to %s' %
                           (remove_count, len(remain_polygons), rm_area_shp))

    polyons_noMulti = [vector_gpd.MultiPolygon_to_polygons(idx, poly) for idx, poly in enumerate(remain_polygons)]
    remain_polygons = []
    for polys in polyons_noMulti:
        polys = [poly for poly in polys if poly.area > min_area]  # remove tiny polygon
        remain_polygons.extend(polys)
    print('convert MultiPolygon to polygons and remove tiny polgyons, remain %d' % (len(remain_polygons)))

    if len(remain_polygons) < 1:
        return False

    save_pd = pd.DataFrame({'Polygon':remain_polygons})
    vector_gpd.save_polygons_to_files(save_pd,'Polygon',wkt,rm_area_shp)
    return rm_area_shp


def remove_based_on_shapeinfo(in_shp, save_shp, max_box_WH):

    shapefile = gpd.read_file(in_shp)
    polygons = shapefile.geometry.values
    # print('\n Polygon Count',len(polygons))
    shape_info_list = [vector_gpd.calculate_polygon_shape_info(item) for item in polygons]

    # save shapeinfo to disk
    shapeinfo_all_dict = vector_gpd.list_to_dict(shape_info_list)
    vector_gpd.add_attributes_to_shp(in_shp, shapeinfo_all_dict)

    #read the shapefile again
    shapefile = gpd.read_file(in_shp)

    # remove relative large but narrow ones.
    remove_count = 0
    for idx, row in shapefile.iterrows():
        shape_info = shape_info_list[idx]

        length = max(shape_info['WIDTH'], shape_info['HEIGHT'])
        if length > max_box_WH:
            shapefile.drop(idx, inplace=True)
            remove_count += 1
            continue
    print('remove %d polygons based on max_box_WH, remain %d' % (remove_count, len(shapefile.geometry.values)))

    if len(shapefile.geometry.values) < 1:
        print('No polygons remain')
        return False

    shapefile.to_file(save_shp, driver='ESRI Shapefile')
    return save_shp

def remove_based_medialAxis(in_shp, save_shp,process_num,max_axis_width):

    # calculate width based on medial axis
    buffer_size = 10    # buffer polgyons, to avoid calulate medial axis failed
    medial_dis_shp = io_function.get_name_by_adding_tail(in_shp, 'medialAxis')
    if os.path.isfile(medial_dis_shp) and vector_gpd.is_field_name_in_shp(medial_dis_shp,'e_max_dis') :
        print('%s exists, skip'%medial_dis_shp)
    else:
        calculate_distance_medial_axis(in_shp,medial_dis_shp,process_num=process_num,enlarge_m=buffer_size)

    # copy medial_dis_shp to in_shp
    copy_attributes = ['e_max_dis'] #  ['e_min_dis', 'e_max_dis','e_mean_dis','e_medi_dis','e_medi_h']
    media_shapefile = gpd.read_file(medial_dis_shp)
    add_values = {}
    for att in copy_attributes:
        attribute_values = media_shapefile[att].tolist()
        attribute_values = [ item - 2*buffer_size for item in attribute_values]
        add_values[att] = attribute_values

    vector_gpd.add_attributes_to_shp(in_shp,add_values)

    b_smaller = False
    vector_gpd.remove_polygons(in_shp,'e_max_dis',max_axis_width,b_smaller,save_shp)
    return True

def extract_headwall_from_slope(idx, total, slope_tif, work_dir, save_dir,slope_threshold, min_area, max_area,max_axis_width,max_box_WH,process_num):
    '''

    :param idx: tif index
    :param total: total slope file count
    :param slope_tif: slope file
    :param work_dir:
    :param save_dir:
    :param slope_threshold:
    :param min_area:
    :param max_area:
    :param max_axis_width: max width based on medial axis
    :param max_box_WH:  max width or height based on minimum_rotated_rectangle
    :param process_num:
    :return:
    '''

    headwall_shp = os.path.splitext(os.path.basename(io_function.get_name_by_adding_tail(slope_tif,'headwall')))[0] + '.shp'
    save_headwall_shp = os.path.join(save_dir,headwall_shp)
    if os.path.isfile(save_headwall_shp):
        print('%s exists, skip'%save_headwall_shp)
        return save_headwall_shp


    print('(%d/%d) extracting headwall from %s'%(idx,total,slope_tif))

    wkt = map_projection.get_raster_or_vector_srs_info_wkt(slope_tif)
    # binary slope
    slope_bin_path = os.path.join(work_dir, os.path.basename(io_function.get_name_by_adding_tail(slope_tif, 'bin')))
    slope_bin_shp = slope_tif_to_slope_shapefile(slope_tif,slope_bin_path,slope_threshold)


    # only keep small, but not too small
    rm_area_shp = io_function.get_name_by_adding_tail(slope_bin_shp, 'rmArea')
    if os.path.isfile(rm_area_shp):
        print('%s exists, skip removing based on area'%rm_area_shp)
    else:
        if remove_based_on_area(slope_bin_shp,min_area,max_area, wkt,rm_area_shp) is False:
            return False

    # add some shape info
    rm_shapeinfo_shp = io_function.get_name_by_adding_tail(slope_bin_shp, 'rmShape')
    if os.path.isfile(rm_shapeinfo_shp):
        print('%s exists, skip removing based on shape'%rm_shapeinfo_shp)
    else:
        if remove_based_on_shapeinfo(rm_area_shp, rm_shapeinfo_shp, max_box_WH) is False:
            return False

    rm_medialAxis_shp = io_function.get_name_by_adding_tail(slope_bin_shp, 'rmMedialAxis')
    if os.path.isfile(rm_medialAxis_shp):
        print('%s exists, skip removing based on Medial Axis')
    else:
        remove_based_medialAxis(rm_shapeinfo_shp, rm_medialAxis_shp,process_num,max_axis_width)

    # copy the results.
    io_function.copy_shape_file(rm_medialAxis_shp,save_headwall_shp)

    # add slope around surrounding? the sourrounding should be flat.  NO.


    return save_headwall_shp

def calculate_distance_medial_axis(input_shp, out_shp, process_num=4, enlarge_m=20):
    print('calculating polygon width based on medial axis')

    code_dir = os.path.expanduser('~/codes/PycharmProjects/ChangeDet_DL/thawSlumpChangeDet')
    sys.path.insert(0, code_dir)

    # after test, found that when polygons are very narrow and irregular, cal_retreat_rate output wrong results.
    # use buffer enlarge the polygons

    polygons = vector_gpd.read_polygons_gpd(input_shp)
    # for poly in polygons:
    #     if poly.geom_type == 'MultiPolygon':
    #         print(poly.geom_type,poly)
    # cal_retreat_rate only use exterior, fill hole for buffer
    # polygon_large = [ vector_gpd.fill_holes_in_a_polygon(item) for item in polygons]
    polygon_large = polygons
    # buffer
    polygon_large = [item.buffer(enlarge_m) for item in polygon_large]

    wkt = map_projection.get_raster_or_vector_srs_info_wkt(input_shp)
    # save_large_shp = io_function.get_name_by_adding_tail(input_shp,'larger')
    save_pd = pd.DataFrame({'Polygon':polygon_large})
    vector_gpd.save_polygons_to_files(save_pd,'Polygon',wkt,out_shp)

    # calculate width based on expanding areas
    import cal_retreat_rate
    if cal_retreat_rate.cal_expand_area_distance(out_shp, proc_num=process_num,save_medial_axis=True):
        os.system('rm save_medial_axis_radius*.txt out_polygon_vertices_*.txt')
        return out_shp

def test_calculate_distance_medial_axis():

    # save polygons without holes
    # proc_num = 16
    # shp = os.path.join('dem_headwall_shp','slope_sub_headwall.shp')

    # after test, found that when polygons are very narrow and irregular, cal_retreat_rate output wrong results.

    proc_num = 1
    shp = os.path.join('dem_headwall_shp','20170611_headwall_test.shp')

    # polygons = vector_gpd.read_polygons_gpd(shp)
    # polygon_nohole = [ vector_gpd.fill_holes_in_a_polygon(item) for item in polygons]
    #
    # wkt = map_projection.get_raster_or_vector_srs_info_wkt(shp)
    # save_nohole_shp = io_function.get_name_by_adding_tail(shp,'nohole')
    # save_pd = pd.DataFrame({'Polygon':polygon_nohole})
    # vector_gpd.save_polygons_to_files(save_pd,'Polygon',wkt,save_nohole_shp)

    out_shp = io_function.get_name_by_adding_tail(shp,'medial_axis')

    #
    calculate_distance_medial_axis(shp,out_shp, process_num=proc_num,enlarge_m=10)



# def test_extract_headwall_from_slope():
#     print('\n')
#     slope = os.path.expanduser('~/Data/tmp_data/slope_sub.tif')
#     working_dir = './test_extract_headwall_from_slope'
#     save_dir = dem_headwall_shp_dir
#     if os.path.isdir(working_dir) is False:
#         io_function.mkdir(working_dir)
#     if os.path.isdir(save_dir) is False:
#         io_function.mkdir(save_dir)
#
#     min_slope = 20
#     min_size = 200
#     max_size = 50000
#     max_axis_width = 80
#     max_box_WH = 600
#     process_num = 10
#
#     extract_headwall_from_slope(0, 1, slope, working_dir, save_dir, min_slope, min_size, max_size,max_axis_width,max_box_WH,process_num)


def main(options, args):
    input = args[0]

    if input.endswith('.txt'):
        slope_tifs = io_function.read_list_from_txt(input)
    elif os.path.isdir(input):
        slope_tifs = io_function.get_file_list_by_ext('.tif',input, bsub_folder=True)
    else:
        slope_tifs = [ input]
    process_num = options.process_num

    working_dir = './'
    save_dir = dem_headwall_shp_dir
    if os.path.isdir(working_dir) is False:
        io_function.mkdir(working_dir)
    if os.path.isdir(save_dir) is False:
        io_function.mkdir(save_dir)

    failed_tifs = []

    min_slope = options.min_slope
    min_size = options.min_area
    max_size = options.max_area
    max_axis_width = options.max_axis_width
    max_box_WH = options.max_box_WH
    for idx, slope in enumerate(slope_tifs):
        if extract_headwall_from_slope(idx, len(slope_tifs), slope,working_dir,save_dir, min_slope,min_size,max_size,max_axis_width,max_box_WH,process_num) is False:
            failed_tifs.append(slope)

    io_function.save_list_to_txt('extract_headwall_failed_tifs.txt',failed_tifs)



if __name__ == '__main__':
    usage = "usage: %prog [options] slopefile or slopefile_list_txt or dir "
    parser = OptionParser(usage=usage, version="1.0 2021-4-30")
    parser.description = 'Introduction: extract RTS headwall from slope derived from ArcticDEM '

    parser.add_option("", "--process_num",
                      action="store", dest="process_num", type=int, default=4,
                      help="number of processes to create the mosaic")

    parser.add_option("-s", "--min_slope",
                      action="store", dest="min_slope", type=float, default=20,
                      help="the minimum slope")

    parser.add_option("", "--min_area",
                      action="store", dest="min_area", type=float, default=200,
                      help="the minimum area")

    parser.add_option("", "--max_area",
                      action="store", dest="max_area", type=float, default=50000,
                      help="the maximum area")

    parser.add_option("", "--max_axis_width",
                      action="store", dest="max_axis_width", type=float, default=80,
                      help="the maximum width based on medial axis")

    parser.add_option("", "--max_box_WH",
                      action="store", dest="max_box_WH", type=float, default=600,
                      help="ax width or height (which is larger) based on minimum_rotated_rectangle")


    (options, args) = parser.parse_args()
    # print(options.create_mosaic)

    if len(sys.argv) < 2 or len(args) < 1:
        parser.print_help()
        sys.exit(2)

    main(options, args)
