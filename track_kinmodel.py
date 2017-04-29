from __future__ import print_function
import numpy as np
import scipy as sp
import numpy.linalg as np_la
import sys
import phasespace.load_mocap as load_mocap
import argparse
import random
import threading
import matplotlib.pyplot as plt
import cProfile
import json
import kinmodel
import ukf
import matplotlib.pyplot as plt
import rospy
import sensor_msgs.msg as sensor_msgs
from std_msgs.msg import Header
import tf
import tf.transformations


FRAMERATE = 50
GROUP_NAME = 'tree'

class KinematicTreeTracker(object):
    def __init__(self, kin_tree, mocap_source, joint_states_topic=None, object_tf_frame=None,
            new_frame_callback=None, return_array=False):
        self.kin_tree = kin_tree
        self.mocap_source = mocap_source
        self._joint_states_pub = None
        self._tf_pub = None
        self._callback = new_frame_callback
        self._return_array = return_array
        self.exit = False

        if joint_states_topic is not None:
            self._joint_states_pub = rospy.Publisher(joint_states_topic, sensor_msgs.JointState,
                    queue_size=10)
        if object_tf_frame is not None:
            self._tf_pub = tf.TransformBroadcaster()

    def start(self):
        self.exit = False
        reader_thread = threading.Thread(target=self.run)
        reader_thread.start()

    def stop(self):
        self.exit = True

    def run(self):
        # Get the base marker indices
        base_indices = []
        base_joint = self.kin_tree.get_root_joint()
        for child in base_joint.children:
            if not hasattr(child, 'children'):
                # This is a feature
                base_indices.append(int(child.name.split('_')[1]))

        # Get all the marker indices of interest
        all_marker_indices = []
        for feature_name in self.kin_tree.get_features():
            all_marker_indices.append(int(feature_name.split('_')[1]))

        # Set the base coordinate transform for the mocap stream
        desired = np.zeros((len(base_indices), 3, 1))
        all_features = self.kin_tree.get_features()
        for i, idx in enumerate(base_indices):
            desired[i,:,0] = all_features['mocap_' + str(idx)].q()
        self.mocap_source.set_coordinates(base_indices, desired, mode='time_varying')

        # Create the observation and measurement models
        test_ss_model = kinmodel.KinematicTreeStateSpaceModel(self.kin_tree)
        measurement_dim = len(test_ss_model.measurement_model(np.array([0.0, 0.0])))
        state_dim = 2

        # Run the filter
        ukf_output = []
        for i, (frame, timestamp) in enumerate(self.mocap_source):
            if self.exit:
                break
            feature_dict = {}
            for marker_idx in all_marker_indices:
                obs_point = kinmodel.new_geometric_primitive(
                        np.concatenate((frame[marker_idx,:,0], np.ones(1))))
                feature_dict['mocap_' + str(marker_idx)] = obs_point
            if i == 0:
                sys.stdout.flush()
                initial_obs = test_ss_model.vectorize_measurement(feature_dict)
                uk_filter = ukf.UnscentedKalmanFilter(test_ss_model.process_model,
                        test_ss_model.measurement_model, np.zeros(2), np.identity(2)*0.25)
                for i in range(50):
                    uk_filter.filter(initial_obs)
            else:
                # print('UKF Step: ' + str(i) + '/' + str(len(ukf_mocap)), end='\r')
                # sys.stdout.flush()
                obs_array = test_ss_model.vectorize_measurement(feature_dict)
                joint_angles = uk_filter.filter(obs_array)[0]
                if self._callback is not None:
                    self._callback(i=i, joint_angles=joint_angles)

                if self._return_array:
                    ukf_output.append(joint_angles)

                if self._joint_states_pub is not None:
                    msg = sensor_msgs.JointState(position=joint_angles.squeeze(),
                            header=Header(stamp=rospy.Time.now()))
                    self._joint_states_pub.publish(msg)

                # Publish the base frame pose of the flexible object
                if self._tf_pub is not None:
                    homog = np_la.inv(self.mocap_source.get_last_coordinates())
                    mocap_frame_name = self.mocap_source.get_frame_name()
                    if mocap_frame_name is not None:
                        self._tf_pub.sendTransform(homog[0:3,3],
                                tf.transformations.quaternion_from_matrix(homog),
                                rospy.Time.now(), '/object_base', '/' + mocap_frame_name)
        if self._return_array:
            ukf_output = np.concatenate(ukf_output, axis=1)
            return ukf_output

# KinematicTreeExternalFrameTracker
#__init__(kin_tree, base_tf_frame_name)
# attach_frame(joint_name, frame_name, tf_pub=True, pose=None): Defaults to origin at mean position of all points on joint
# attach_tf_frame(joint_name, frame_name, tf_frame_name): Attaches frame and adds frame to update list
# set_config(joint_angles_dict)
# observe_frames(): returns dict of transforms of all external frames (even TF ones)
# compute_jacobian(base_frame_name, manip_frame_name): returns dict of twists

class KinematicTreeExternalFrameTracker(object):
    def __init__(self, kin_tree, base_tf_frame_name):
        self._kin_tree = kin_tree
        self._base_frame_name = base_tf_frame_name
        self._tf_pub_frame_names = [] # Frame names to publish on tf after an _update() call
        self._attached_frame_names = [] # All attached static frames
        self._attached_tf_frame_names = [] # All attached tf frames - update during an _update() call
        self._tf_pub = tf.TransformBroadcaster()
        self._tf_listener = tf.TransformListener()
        self._frames_stale = True

    def attach_frame(self, joint_name, frame_name, tf_pub=True, pose=None):
        self._frames_stale = True
        # Attach a static frame to the tree
        joints = kin_tree.get_joints()
        if pose is None:
            # No pose specified, set to mean position of all other Point children of this joint
            trans = np.zeros((3,))
            for point in joints[joint_name].children:
                trans = trans + point.primitive.q().squeeze()
            trans = trans / len(joints[joint_name].children)
            homog = np.identity(4)
            homog[0:3,3] = trans
        else:
            # Attach the frame at the specified pose
            homog = pose.squeeze()
        new_feature = kinmodel.Feature(frame_name, kinmodel.Transform(homog_array=homog))
        joints[joint_name].children.append(new_feature)
        self._attached_frame_names.append(frame_name)
        if tf_pub:
            self._tf_pub_frame_names.append(frame_name)

    def attach_tf_frame(self, joint_name, tf_frame_name):
        self._frames_stale = True
        joints = kin_tree.get_joints()
        new_feature = kinmodel.Feature(tf_frame_name, kinmodel.Transform())
        joints[joint_name].children.append(new_feature)
        self._attached_tf_frame_names.append(tf_frame_name)

    def set_config(self, joint_angles_dict):
        self._frames_stale = True
        self._kin_tree.set_config(joint_angles_dict)

    def observe_frames(self, ):
        self._update()
        observations = self._kin_tree.observe_features()
        external_frame_dict = {}
        for frame_name in self._attached_frame_names:
            external_frame_dict[frame_name] = observations[frame_name]
        for frame_name in self._attached_tf_frame_names:
            external_frame_dict[frame_name] = observations[frame_name]
        return external_frame_dict

    def compute_jacobian(self, base_frame_name, manip_name_frame):
        self._update()
        return self._kin_tree.compute_jacobian(base_frame_name, manip_name_frame)

    def _update(self):
        if self._frames_stale:
            self._frames_stale = False
            # Update tf frame poses
            for frame_name in self._attached_tf_frame_names:
                new_features = {}
                try:
                    trans, rot = self._tf_listener.lookupTransform(self._base_frame_name, frame_name, rospy.Time(0))
                    tf_transform = tf.transformations.quaternion_matrix(rot)
                    tf_transform[0:3,3] = trans
                    new_features[frame_name] = kinmodel.Transform(homog_array=tf_transform)
                except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                    print('Lookup failed for TF frame: ' + frame_name)
            self._kin_tree.set_features(new_features)

            # Observe and publish specified static features
            feature_obs = self._kin_tree.observe_features()
            for frame_name in self._tf_pub_frame_names:
                obs = feature_obs[frame_name].homog()
                self._tf_pub.sendTransform(obs[0:3,3],
                                        tf.transformations.quaternion_from_matrix(obs),
                                        rospy.Time.now(), frame_name, self.base_frame_name)


def main():
    rospy.init_node('kin_tree_tracker')
    plt.ion()
    parser = argparse.ArgumentParser()
    parser.add_argument('kinmodel_json_optimized', help='The kinematic model JSON file')
    # parser.add_argument('mocap_npz')
    args = parser.parse_args()

    #Load the calibration sequence
    # calib_data = np.load(args.mocap_npz)
    # ukf_mocap = load_mocap.MocapArray(calib_data['full_sequence'][:,:,:], FRAMERATE)

    # Load the mocap stream
    ukf_mocap = load_mocap.PointCloudStream('/mocap_point_cloud')
    tracker_kin_tree = kinmodel.KinematicTree(json_filename=args.kinmodel_json_optimized)
    kin_tree = kinmodel.KinematicTree(json_filename=args.kinmodel_json_optimized)

    #Set up the jacobian computation
    joints = kin_tree.get_joints()
    children2 = joints['joint2'].children
    trans2 = np.zeros((3,))
    for point in children2:
        trans2 = trans2 + point.primitive.q().squeeze()
    trans2 = trans2 / len(children2)
    homog2 = np.identity(4)
    homog2[0:3,3] = trans2
    trans2 = kinmodel.Transform(homog_array=homog2)
    children2.append(kinmodel.Feature('trans2', trans2))

    children3 = joints['joint3'].children
    trans3 = np.zeros((3,))
    for point in children3:
        trans3 = trans3 + point.primitive.q().squeeze()
    trans3 = trans3 / len(children3)
    homog3 = np.identity(4)
    homog3[0:3,3] = trans3
    trans3 = kinmodel.Transform(homog_array=homog3)
    children3.append(kinmodel.Feature('trans3', trans3))

    tf_pub = tf.TransformBroadcaster()
    tf_listener = tf.TransformListener()

    def new_frame_callback(i, joint_angles):
        # print('Frame:' + str(i) + ', State:' + str(joint_angles.squeeze()), end='\r')
        # sys.stdout.flush()
        try:
            kin_tree.set_config({'joint2':joint_angles[0], 'joint3':joint_angles[1]})
            
            feature_obs = kin_tree.observe_features()

            obs2 = feature_obs['trans2'].homog()
            tf_pub.sendTransform(obs2[0:3,3],
                                    tf.transformations.quaternion_from_matrix(obs2),
                                    rospy.Time.now(), '/trans2', '/object_base')

            obs3 = feature_obs['trans3'].homog()
            tf_pub.sendTransform(obs3[0:3,3],
                                    tf.transformations.quaternion_from_matrix(obs3),
                                    rospy.Time.now(), '/trans3', '/object_base')
            trans, rot = tf_listener.lookupTransform('/trans3', '/left_hand', rospy.Time(0))
            
            tf_pub.sendTransform(trans,
                                    rot,
                                    rospy.Time.now(), '/trans4', '/trans3')

            grasp_transform = tf.transformations.quaternion_matrix(rot)
            grasp_transform[0:3,3] = trans

            jacobian = kin_tree.compute_jacobian('trans2', 'trans3', 
                grasp_transform=kinmodel.Transform(homog_array=grasp_transform))
            print(jacobian)


        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
            print('lookup failed')



    tracker = KinematicTreeTracker(tracker_kin_tree, ukf_mocap, joint_states_topic='/kinmodel_state',
            object_tf_frame='/object_base', new_frame_callback=new_frame_callback)
    # ukf_output = tracker.run()
    tracker.start()
    rospy.spin()
    tracker.stop()


    # plt.plot(ukf_output.T)
    # plt.pause(100)

    
if __name__ == '__main__':
    # cProfile.run('main()', 'fit_kinmodel.profile')
    main()
