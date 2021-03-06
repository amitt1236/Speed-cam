import math
import time

import cv2
import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
from absl import app, flags
from absl.flags import FLAGS
from tensorflow.python.saved_model import tag_constants

import core.utils as utils
from core.config import cfg
# deep sort imports
from deep_sort import preprocessing, nn_matching
from deep_sort.detection import Detection
from deep_sort.tracker import Tracker
from tools import generate_detections as gdet

# gpu setting
physical_devices = tf.config.experimental.list_physical_devices('GPU')
if len(physical_devices) > 0:
    tf.config.experimental.set_memory_growth(physical_devices[0], True)

# Flags
flags.DEFINE_string('weights', './tf_model/yolov4-512', 'path to weights file')
flags.DEFINE_integer('size', 512, 'resize images to')
flags.DEFINE_boolean('tiny', False, 'yolo or yolo-tiny')
flags.DEFINE_string('model', 'yolov4', 'yolov3 or yolov4')
flags.DEFINE_string('video', './video/5.mov', 'path to input video or set to 0 for webcam')
flags.DEFINE_string('output', './video/5-out.avi', 'path to output video')
flags.DEFINE_string('output_format', 'XVID', 'codec used in VideoWriter when saving video to file')
flags.DEFINE_float('iou', 0.45, 'iou threshold')
flags.DEFINE_float('score', 0.50, 'score threshold')
flags.DEFINE_boolean('dont_show', False, 'dont show video output')
flags.DEFINE_boolean('info', False, 'show detailed info of tracked objects')
flags.DEFINE_boolean('count', False, 'count objects being tracked on screen')

# speed array
start_timing = np.zeros(1000)
ending = np.zeros(1000)
speed = np.zeros(1000)
road_slope = -0.23
flag = np.empty(1000, bool)
flag.fill(False)


def main(_argv):
    # Definition of the parameters
    max_cosine_distance = 0.4
    nn_budget = None
    nms_max_overlap = 1.0

    # initialize deep sort
    model_filename = 'model_data/mars-small128.pb'
    encoder = gdet.create_box_encoder(model_filename, batch_size=1)
    metric = nn_matching.NearestNeighborDistanceMetric("cosine", max_cosine_distance, nn_budget)
    tracker = Tracker(metric)

    # load configuration for object detector
    input_size = FLAGS.size
    video_path = FLAGS.video

    saved_model_loaded = tf.saved_model.load(FLAGS.weights, tags=[tag_constants.SERVING])
    infer = saved_model_loaded.signatures['serving_default']

    # video capture
    vid = cv2.VideoCapture(video_path)
    # frame counter
    frame_num = 0

    # create writer
    if FLAGS.output:
        width = int(vid.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(vid.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = int(vid.get(cv2.CAP_PROP_FPS))
        codec = cv2.VideoWriter_fourcc(*FLAGS.output_format)
        out = cv2.VideoWriter(FLAGS.output, codec, fps, (width, height))

    # while video is running
    while True:
        ret, frame = vid.read()
        # no video input
        if not ret:
            print('End of video')
            break
        # converting to rgb for yolo model
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # frame count
        frame_num += 1
        start_time = time.time()
        print('Frame #: ', frame_num)

        # resize to yolo input size
        image_data = cv2.resize(frame, (input_size, input_size))
        image_data = image_data / 255.
        image_data = image_data[np.newaxis, ...].astype(np.float32)

        # feeding the model with batch of one
        batch_data = tf.constant(image_data)
        pred_bbox = infer(batch_data)
        for key, value in pred_bbox.items():
            boxes = value[:, :, 0:4]
            pred_conf = value[:, :, 4:]

        # bounding boxes process
        boxes, scores, classes, valid_detections = tf.image.combined_non_max_suppression(
            boxes=tf.reshape(boxes, (tf.shape(boxes)[0], -1, 1, 4)),
            scores=tf.reshape(
                pred_conf, (tf.shape(pred_conf)[0], -1, tf.shape(pred_conf)[-1])),
            max_output_size_per_class=50,
            max_total_size=50,
            iou_threshold=FLAGS.iou,
            score_threshold=FLAGS.score
        )

        # convert data to numpy arrays and slice out unused elements
        num_objects = valid_detections.numpy()[0]
        bboxes = boxes.numpy()[0]
        bboxes = bboxes[0:int(num_objects)]
        scores = scores.numpy()[0]
        scores = scores[0:int(num_objects)]
        classes = classes.numpy()[0]
        classes = classes[0:int(num_objects)]

        # format bounding boxes from normalized ymin, xmin, ymax, xmax ---> xmin, ymin, width, height
        original_h, original_w, _ = frame.shape
        bboxes = utils.format_boxes(bboxes, original_h, original_w)

        # read in all class names from config
        class_names = utils.read_class_names(cfg.YOLO.CLASSES)

        # custom allowed classes
        allowed_classes = ['car', 'truck', 'bus', 'motorcycle']

        # loop through objects and use class index to get class name, allow only classes in allowed_classes list
        names = []
        deleted_indx = []
        for i in range(num_objects):
            class_indx = int(classes[i])
            class_name = class_names[class_indx]
            if class_name not in allowed_classes:
                deleted_indx.append(i)
            else:
                names.append(class_name)
        names = np.array(names)
        count = len(names)
        if FLAGS.count:
            cv2.putText(frame, "Objects being tracked: {}".format(count), (5, 35), cv2.FONT_HERSHEY_COMPLEX_SMALL, 2,
                        (0, 255, 0), 2)
            print("Objects being tracked: {}".format(count))
        # delete detections that are not in allowed_classes
        bboxes = np.delete(bboxes, deleted_indx, axis=0)
        scores = np.delete(scores, deleted_indx, axis=0)

        # encode yolo detections and feed to tracker
        features = encoder(frame, bboxes)
        detections = [Detection(bbox, score, class_name, feature) for bbox, score, class_name, feature in
                      zip(bboxes, scores, names, features)]

        # initialize color map
        cmap = plt.get_cmap('tab20b')
        colors = [cmap(i)[:3] for i in np.linspace(0, 1, 20)]

        # run non-maxima suppression
        boxs = np.array([d.tlwh for d in detections])
        scores = np.array([d.confidence for d in detections])
        classes = np.array([d.class_name for d in detections])
        indices = preprocessing.non_max_suppression(boxs, classes, nms_max_overlap, scores)
        detections = [detections[i] for i in indices]

        # Call the tracker
        tracker.predict()
        tracker.update(detections)

        # update tracks
        for track in tracker.tracks:
            if not track.is_confirmed() or track.time_since_update > 1:
                continue
            bbox = track.to_tlbr()
            class_name = track.get_class()

            # draw bounding box on screen
            color = colors[int(track.track_id) % len(colors)]
            color = [i * 255 for i in color]
            cv2.rectangle(frame, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), color, 2)
            cv2.rectangle(frame, (int(bbox[0]), int(bbox[1] - 30)),
                          (int(bbox[0]) + (len(class_name) + len(str(track.track_id))) * 17, int(bbox[1])), color, -1)
            cv2.putText(frame, class_name + "-" + str(track.track_id), (int(bbox[0]), int(bbox[1] - 10)), 0, 0.75,
                        (255, 255, 255), 2)

            # y - mx = y_1 - mx_1
            # Lower line: y - 0.55x = 26.67
            # upper line: y - 0.44x = 0.38
            # driving direction = -0.23

            if not flag[track.track_id]:
                start_timing[track.track_id] = frame_num
                ending[track.track_id] = destination(road_slope, bbox[0], bbox[1])
                flag[track.track_id] = True
                if track.track_id == 3:
                    up = upper_vert(road_slope, bbox[0], bbox[1])
                    down = lower_vert(road_slope, bbox[0], bbox[1])
                    p1 = (int(up[0]),int(up[1]))
                    p2 = (int(down[0]), int(down[1]))
                    cv2.line(frame, p1, p2,(0, 0, 255), 2)
                    cv2.line(frame, (0,int(ending[track.track_id])), (int(frame[0].size - 100), int((road_slope * frame[0].size - 100) + ending[track.track_id])), (0, 0, 255), 2)
            elif bbox[1] < ((bbox[0] * road_slope) + ending[track.track_id]):
                if start_timing[track.track_id] > frame_num - 10:
                    speed[track.track_id] = -1
                else:
                    speed[track.track_id] = 4.8 / ((frame_num - start_timing[track.track_id]) * 1 / 59) * 3.6
                flag[track.track_id] = False
            if speed[track.track_id] > 0:
                cv2.putText(frame, 'speed' + str(int(speed[track.track_id])), (int(bbox[0]), int(bbox[1] - 40)), 0,
                            0.75,
                            (255, 255, 255), 2)

            # if enable info flag then print details about each track
            if FLAGS.info:
                print("Tracker ID: {}, Class: {},  BBox Coords (xmin, ymin, xmax, ymax): {}".format(str(track.track_id),
                                                                                                    class_name, (
                                                                                                        int(bbox[0]),
                                                                                                        int(bbox[1]),
                                                                                                        int(bbox[2]),
                                                                                                        int(bbox[3]))))

        # calculate frames per second of running detections
        fps = 1.0 / (time.time() - start_time)
        print("FPS: %.2f" % fps)
        result = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        if not FLAGS.dont_show:
            cv2.imshow("Output Video", result)

        # if output flag is set, save video file
        if FLAGS.output:
            out.write(result)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    vid.release()
    cv2.destroyAllWindows()

    # Upper and vert intersection


def upper_vert(m, x1, y1):
    A = np.array([[1, -m], [1, -0.44]])
    B = np.array([y1 - (m * x1), 7.7])
    return np.flip(np.linalg.solve(A, B))

    # lower and vert intersection


def lower_vert(m, x1, y1):
    A = np.array([[1, -m], [1, -0.55]])
    B = np.array([y1 - (m * x1), 59])
    return np.flip(np.linalg.solve(A, B))


# distance between two points

def distance(p1, p2):
    dist = p1 - p2
    return math.sqrt(np.dot(dist, dist))


def destination(m, x1, y1):
    p1 = upper_vert(m, x1, y1)
    p2 = lower_vert(m, x1, y1)
    b = p2[1] - (p2[0] * m)
    b -= distance(p1, p2) * 0.5
    return b


if __name__ == '__main__':
    try:
        app.run(main)
    except SystemExit:
        pass
