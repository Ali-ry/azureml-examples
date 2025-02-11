import argparse
import os
import json
import numpy as np
import PIL.Image as Image
import xml.etree.ElementTree as ET

from simplification.cutil import simplify_coords
from skimage import measure


def convert_mask_to_polygon(
    mask,
    max_polygon_points=100,
    score_threshold=0.5,
    max_refinement_iterations=25,
    edge_safety_padding=1,
):
    """Convert a numpy mask to a polygon outline in normalized coordinates.

    :param mask: Pixel mask, where each pixel has an object (float) score in [0, 1], in size ([1, height, width])
    :type: mask: <class 'numpy.array'>
    :param max_polygon_points: Maximum number of (x, y) coordinate pairs in polygon
    :type: max_polygon_points: Int
    :param score_threshold: Score cutoff for considering a pixel as in object.
    :type: score_threshold: Float
    :param max_refinement_iterations: Maximum number of times to refine the polygon
    trying to reduce the number of pixels to meet max polygon points.
    :type: max_refinement_iterations: Int
    :param edge_safety_padding: Number of pixels to pad the mask with
    :type edge_safety_padding: Int
    :return: normalized polygon coordinates
    :rtype: list of list
    """
    # Convert to numpy bitmask
    mask = mask[0]
    mask_array = np.array((mask > score_threshold), dtype=np.uint8)
    image_shape = mask_array.shape

    # Pad the mask to avoid errors at the edge of the mask
    embedded_mask = np.zeros(
        (
            image_shape[0] + 2 * edge_safety_padding,
            image_shape[1] + 2 * edge_safety_padding,
        ),
        dtype=np.uint8,
    )
    embedded_mask[
        edge_safety_padding : image_shape[0] + edge_safety_padding,
        edge_safety_padding : image_shape[1] + edge_safety_padding,
    ] = mask_array

    # Find Image Contours
    contours = measure.find_contours(embedded_mask, 0.5)
    simplified_contours = []

    for contour in contours:

        # Iteratively reduce polygon points, if necessary
        if max_polygon_points is not None:
            simplify_factor = 0
            while (
                len(contour) > max_polygon_points
                and simplify_factor < max_refinement_iterations
            ):
                contour = simplify_coords(contour, simplify_factor)
                simplify_factor += 1

        # Convert to [x, y, x, y, ....] coordinates and correct for padding
        unwrapped_contour = [0] * (2 * len(contour))
        unwrapped_contour[::2] = np.ceil(contour[:, 1]) - edge_safety_padding
        unwrapped_contour[1::2] = np.ceil(contour[:, 0]) - edge_safety_padding

        simplified_contours.append(unwrapped_contour)

    return _normalize_contour(simplified_contours, image_shape)


def _normalize_contour(contours, image_shape):

    height, width = image_shape[0], image_shape[1]

    for contour in contours:
        contour[::2] = [x * 1.0 / width for x in contour[::2]]
        contour[1::2] = [y * 1.0 / height for y in contour[1::2]]

    return contours


def binarise_mask(mask_fname):

    mask = Image.open(mask_fname)
    mask = np.array(mask)
    # instances are encoded as different colors
    obj_ids = np.unique(mask)
    # first id is the background, so remove it
    obj_ids = obj_ids[1:]

    # split the color-encoded mask into a set of binary masks
    binary_masks = mask == obj_ids[:, None, None]
    return binary_masks


def parsing_mask(mask_fname):

    # For this particular dataset, initially each mask was merged (based on binary mask of each object)
    # in the order of the bounding boxes described in the corresponding PASCAL VOC annotation file.
    # Therefore, we have to extract each binary mask which is in the order of objects in the annotation file.
    # https://github.com/microsoft/computervision-recipes/blob/master/utils_cv/detection/dataset.py
    binary_masks = binarise_mask(mask_fname)
    polygons = []
    for bi_mask in binary_masks:

        if len(bi_mask.shape) == 2:
            bi_mask = bi_mask[np.newaxis, :]
        polygon = convert_mask_to_polygon(bi_mask)
        polygons.append(polygon)

    return polygons


def convert_mask_in_VOC_to_jsonl(base_dir, workspace):

    src = base_dir
    train_validation_ratio = 5

    # Retrieving default datastore that got automatically created when we setup a workspace
    workspaceblobstore = workspace.get_default_datastore().name

    # Path to the annotations
    annotations_folder = os.path.join(src, "annotations")
    mask_folder = os.path.join(src, "segmentation-masks")

    # Path to the training and validation files
    train_annotations_file = os.path.join(src, "train_annotations.jsonl")
    validation_annotations_file = os.path.join(src, "validation_annotations.jsonl")

    # sample json line dictionary
    json_line_sample = {
        "image_url": "AmlDatastore://"
        + workspaceblobstore
        + "/"
        + os.path.basename(os.path.dirname(src))
        + "/"
        + "images",
        "image_details": {"format": None, "width": None, "height": None},
        "label": [],
    }

    # Read each annotation and convert it to jsonl line
    with open(train_annotations_file, "w") as train_f:
        with open(validation_annotations_file, "w") as validation_f:
            for i, filename in enumerate(os.listdir(annotations_folder)):
                if filename.endswith(".xml"):
                    print("Parsing " + os.path.join(src, filename))

                    root = ET.parse(
                        os.path.join(annotations_folder, filename)
                    ).getroot()

                    width = int(root.find("size/width").text)
                    height = int(root.find("size/height").text)
                    # convert mask into polygon
                    mask_fname = os.path.join(mask_folder, filename[:-4] + ".png")
                    polygons = parsing_mask(mask_fname)

                    labels = []
                    for index, object in enumerate(root.findall("object")):
                        name = object.find("name").text
                        isCrowd = int(object.find("difficult").text)
                        labels.append(
                            {
                                "label": name,
                                "bbox": "null",
                                "isCrowd": isCrowd,
                                "polygon": polygons[index],
                            }
                        )

                    # build the jsonl file
                    image_filename = root.find("filename").text
                    _, file_extension = os.path.splitext(image_filename)
                    json_line = dict(json_line_sample)
                    json_line["image_url"] = (
                        json_line["image_url"] + "/" + image_filename
                    )
                    json_line["image_details"]["format"] = file_extension[1:]
                    json_line["image_details"]["width"] = width
                    json_line["image_details"]["height"] = height
                    json_line["label"] = labels

                    if i % train_validation_ratio == 0:
                        # validation annotation
                        validation_f.write(json.dumps(json_line) + "\n")
                    else:
                        # train annotation
                        train_f.write(json.dumps(json_line) + "\n")
                else:
                    print("Skipping unknown file: {}".format(filename))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument(
        "--data_path",
        type=str,
        help="the directory contains images, annotations, and masks",
    )

    args, remaining_args = parser.parse_known_args()
    data_path = args.data_path

    convert_mask_in_VOC_to_jsonl(data_path)
