import rospy
import cv2
import math
import time
import numpy as np
from scipy.linalg import expm

from geometry_msgs.msg import Twist
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64
from sensor_msgs.msg import LaserScan
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

import tf
from tf.transformations import euler_from_quaternion

from phantomx_gazebo.msg import Rifts

def euler_mat(phi, theta, psi):
    Ad_i = np.array([[0, 0, 0],[0,0,-1],[0,1,0]])
    Ad_j = np.array([[0,0,1],[0,0,0],[-1,0,0]])
    Ad_k = np.array([[0,-1,0],[1,0,0],[0,0,0]])
    M = np.dot(np.dot(expm(psi*Ad_k), expm(theta*Ad_j)), expm(psi*Ad_i))
    return(M)


class PhantomX:
    """Client ROS class for manipulating PhantomX in Gazebo"""

    def __init__(self, ns='/phantomx/', KP = 0.5, KI = 0.1):
        self.ns = ns
        self.joints = None
        self.angles = None

        self._sub_joints = rospy.Subscriber(
            ns + 'joint_states', JointState, self._cb_joints, queue_size=1)
        rospy.loginfo('Waiting for joints to be populated...')
        while not rospy.is_shutdown():
            if self.joints is not None:
                break
            rospy.sleep(0.1)
            rospy.loginfo('Waiting for joints to be populated...')
        rospy.loginfo('Joints populated')

        rospy.loginfo('Creating joint command publishers')
        self._pub_joints = {}
        for j in self.joints:
            p = rospy.Publisher(
                ns + j + '_position_controller/command', Float64, queue_size=1)
            self._pub_joints[j] = p
            
        self._pub_cmd_vel = rospy.Publisher(ns + 'cmd_vel', Twist, queue_size=1)
        self._pub_rifts_coords = rospy.Publisher(ns + 'rifts_coord', Rifts, queue_size=1)    
        self._bridge = CvBridge()
        self._image_sub = rospy.Subscriber("/hexabot/camera/image_raw", Image, self.camera_callback)

        rospy.Subscriber('/scan', LaserScan, self._callback_scan)
        self.ranges = [0]*360
        self.KP = KP
        self.KI = KI
        self.time = float(rospy.Time.to_sec(rospy.Time.now()))
        
        self.listener = tf.TransformListener()



    def set_walk_velocity(self, x, y, t):
        msg = Twist()
        msg.linear.x = x
        msg.linear.y = y
        msg.angular.z = t
        self._pub_cmd_vel.publish(msg)
        
    def camera_callback(self, data):
        cv_image = self._bridge.imgmsg_to_cv2(data, "bgr8")
        cv_blur = cv2.GaussianBlur(cv_image,(5,5),0)
        cv_denoise = cv2.medianBlur(cv_blur, 5)
        cv_edges = cv2.Canny(cv_denoise,6,16)
        distance = np.mean(self.ranges[80:110])
        horizontal_fov = 0.616
        pixel_size = 2*distance*math.tan(horizontal_fov/2)/cv_edges.shape[1]
        coords = np.argwhere(cv_edges>0)*pixel_size
        
        (trans,rot) = self.listener.lookupTransform('/map', '/base_link', rospy.Time(0))

        rot = euler_from_quaternion(rot)
        rot = euler_mat(rot[0], rot[1], rot[2])

        msg = Rifts()
        r = rospy.Rate(1)
        coords = np.array([coords[:, 0], [-distance]*len(coords), coords[:, 1]])
        # rospy.loginfo(coords)
        # rospy.loginfo(rot)
        # rospy.loginfo(trans)
        coord_rifts_map=[]
        if coords.shape[1]>0:
            coord_rifts_map= np.dot(rot, coords)
            coord_rifts_map[0, :]+=trans[0]
            coord_rifts_map[1,:]+=trans[1]
            coord_rifts_map[2,:]+=trans[2]
            msg.x = coord_rifts_map[0,:]
            msg.y = coord_rifts_map[1,0]
            msg.z = coord_rifts_map[2,:]
        else:
            msg.x = []
            msg.y = -1
            msg.z = []
            
        # msg.x = coords[:,0]
        # msg.y = -distance
        # msg.z = coords[:,1]

        self._pub_rifts_coords.publish(msg)
        r.sleep()

    def _callback_scan(self, msg):  
        self.ranges = msg.ranges
        self.now = float(rospy.Time.to_sec(rospy.Time.now()))
        

    def _cb_joints(self, msg):
        if self.joints is None:
            self.joints = msg.name
        self.angles = msg.position

    def get_angles(self):
        if self.joints is None:
            return None
        if self.angles is None:
            return None
        return dict(zip(self.joints, self.angles))

    def set_angles(self, angles):
        for j, v in angles.items():
            if j not in self.joints:
                rospy.logerror('Invalid joint name "' + j + '"')
                continue
            self._pub_joints[j].publish(v)

    def set_angles_slow(self, stop_angles, delay=2):
        start_angles = self.get_angles()
        start = time.time()
        stop = start + delay
        r = rospy.Rate(100)
        while not rospy.is_shutdown():
            t = time.time()
            if t > stop:
                break
            ratio = (t - start) / delay
            angles = interpolate(stop_angles, start_angles, ratio)
            self.set_angles(angles)
            r.sleep()
            

    def follow_wall(self):
        val1, val2 = np.mean(self.ranges[50:80]), np.mean(self.ranges[280:310])
        e = val1 - val2

        delta_t = (self.now - self.time)
        self.time = self.now

        P = e
        I = e*delta_t
        z = self.KI*I + self.KP*P

        saturation = 0.4
        # z = (z > saturation)*saturation + (z < -saturation)*(-saturation) + ((z <= saturation) and (z >= -saturation))*z
        z = min(max(z, -saturation), saturation)

        return z


def interpolate(anglesa, anglesb, coefa):
    z = {}
    joints = anglesa.keys()
    for j in joints:
        z[j] = anglesa[j] * coefa + anglesb[j] * (1 - coefa)
    return z


def get_distance(anglesa, anglesb):
    d = 0
    joints = anglesa.keys()
    if len(joints) == 0:
        return 0
    for j in joints:
        d += abs(anglesb[j] - anglesa[j])
    d /= len(joints)
    return d
