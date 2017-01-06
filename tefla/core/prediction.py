from __future__ import division, print_function, absolute_import

import time
from scipy.stats.mstats import gmean
import numpy as np
import tensorflow as tf
from tefla.da import tta
from tefla.utils import util


class PredictSessionMixin(object):
    """
    base mixin class for prediction
    """

    def __init__(self, weights_from):
        self.weights_from = weights_from

    def predict(self, X):
        """
        Returns the network predcitions for a single or batch of inputs
        """
        saver = tf.train.Saver()
        with tf.Session() as sess:
            saver.restore(sess, self.weights_from)
            return self._real_predict(X, sess)

    def _real_predict(self, X, sess):
        pass


class OneCropPredictor(PredictSessionMixin):
    """One crop Predictor, it predict network out put from a single crop of an input image

    Args:
        model: model definition file
        cnf: prediction configs
        weights_from: location of the model weights file
        prediction_iterator: iterator to access and augment the data for prediction
    """

    def __init__(self, model, cnf, weights_from, prediction_iterator):
        self.model = model
        self.cnf = cnf
        self.prediction_iterator = prediction_iterator

        end_points_predict = model(is_training=False, reuse=None)
        self.inputs = end_points_predict['inputs']
        self.predictions = end_points_predict['predictions']
        super(OneCropPredictor, self).__init__(weights_from)

    def _real_predict(self, X, sess, xform=None, crop_bbox=None):
        tic = time.time()
        print('Making %d predictions' % len(X))
        data_predictions = []
        for X, y in self.prediction_iterator(X, xform=xform, crop_bbox=crop_bbox):
            predictions_e = sess.run(self.predictions, feed_dict={self.inputs: X})
            data_predictions.append(predictions_e)
        data_predictions = np.vstack(data_predictions)
        print('took %6.1f seconds' % (time.time() - tic))
        return data_predictions


class QuasiPredictor(PredictSessionMixin):
    """Quasi transform predictor

    Args:
        model: model definition file
        cnf: prediction configs
        weights_from: location of the model weights file
        prediction_iterator: iterator to access and augment the data for prediction
        number_of_transform: number of determinastic augmentaions to be performed on the input data
            resulted predictions are averaged over the augmentated transformation prediction outputs
    """

    def __init__(self, model, cnf, weights_from, prediction_iterator, number_of_transforms):
        self.number_of_transforms = number_of_transforms
        self.cnf = cnf
        self.prediction_iterator = prediction_iterator
        self.predictor = OneCropPredictor(model, cnf, weights_from, prediction_iterator)
        super(QuasiPredictor, self).__init__(weights_from)

    def _real_predict(self, X, sess):
        standardizer = self.prediction_iterator.standardizer
        da_params = standardizer.da_processing_params()
        util.veryify_args(da_params, ['sigma'], 'QuasiPredictor > standardizer does unknown da with param(s):')
        color_sigma = da_params.get('sigma', 0.0)
        tfs, color_vecs = tta.build_quasirandom_transforms(self.number_of_transforms, color_sigma=color_sigma,
                                                           **self.cnf['aug_params'])
        multiple_predictions = []
        for i, (xform, color_vec) in enumerate(zip(tfs, color_vecs), start=1):
            print('Quasi-random tta iteration: %d' % i)
            standardizer.set_tta_args(color_vec=color_vec)
            predictions = self.predictor._real_predict(X, sess, xform=xform)
            multiple_predictions.append(predictions)
        return np.mean(multiple_predictions, axis=0)


class CropPredictor(PredictSessionMixin):
    """Multiples non Data augmented crops predictor

    Args:
        model: model definition file
        cnf: prediction configs
        weights_from: location of the model weights file
        prediction_iterator: iterator to access and augment the data for prediction
        crop_size: crop size for network input
        im_size: original image size
        number_of_crops: total number of crops to extract from the input image
        """

    def __init__(self, model, cnf, weights_from, prediction_iterator, crop_size, im_size, number_of_crops=10):
        self.number_of_crops = number_of_crops
        self.cnf = cnf
        self.prediction_iterator = prediction_iterator
        self.predictor = OneCropPredictor(model, cnf, weights_from, prediction_iterator)
        self.crop_size = np.array((crop_size, crop_size))
        self.im_size = np.array((im_size, im_size))
        super(CropPredictor, self).__init__(weights_from)

    def _real_predict(self, X, sess):
        if self.number_of_crops > 1:
            bboxs = util.get_bbox_10crop(self.crop_size, self.im_size)
            multiple_predictions = []
            for i, bbox in enumerate(bboxs, start=1):
                print('Crop-determinastic iteration: %d' % i)
                predictions = self.predictor._real_predict(X, sess, crop_bbox=bbox)
                multiple_predictions.append(predictions)
            return np.mean(multiple_predictions, axis=0)
        elif self.number_of_crops == 1:
            predictions = self.predictor._real_predict(X, sess)
            return predictions


class EnsemblePredictor(object):
    """Returns predcitions from multiples models

    Ensembled predictions from multiples models using ensemble type

    Args:
        predictors: predictor instances
    """

    def __init__(self, predictors):
        self.predictors = predictors

    def predict(self, X, ensemble_type='mean'):
        """
        Returns ensembled predictions for an input or batch of inputs

        Args:
            X: 4D tensor, inputs
            ensemble_type: operation to combine models probabilities
                    available type: ['mean', 'gmean', 'log_mean']
        """
        multiple_predictions = []
        for p in self.predictors:
            print('Ensembler - running predictions using: %s' % p)
            predictions = p.predict(X)
            multiple_predictions.append(predictions)
        multiple_predictions = np.array(multiple_predictions, dtype=np.float32)
        return _ensemble(ensemble_type, multiple_predictions)


def _ensemble(en_type, x):
    return {
        'mean': np.mean(x, axis=0),
        'gmean': gmean(x, axis=0),
        'log_mean': np.mean(np.log(x + (x==0)), axis=0),
    }[en_type]