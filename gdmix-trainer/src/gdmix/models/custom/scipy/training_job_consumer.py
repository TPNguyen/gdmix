import logging
from multiprocessing.queues import Queue

import numpy as np
from collections import namedtuple
from gdmix.models.custom.binary_logistic_regression import BinaryLogisticRegressionTrainer

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Create a named tuple to represent training result
TrainingResult = namedtuple("TrainingResult", "training_result unique_global_indices")


class TrainingJobConsumer:
    """Callable class to consume entity-based random effect training jobs from a shared queue"""
    _CONSUMER_LOGGING_FREQUENCY = 1000

    def __init__(self, consumer_id, training_results: dict, regularize_bias=False, lambda_l2=1.0, tolerance=1e-8, num_of_curvature_pairs=10, num_iterations=100,
                 timeout_in_seconds=300):
        """
        :param training_results:   Shared dictionary to transfer training results
        :param timeout_in_seconds:   Timeout (in seconds) for retrieving items off the shared job queue
        """
        self.timeout_in_seconds = timeout_in_seconds
        self.training_results = training_results
        self.consumer_id = consumer_id
        self.lr_trainer = BinaryLogisticRegressionTrainer(regularize_bias=regularize_bias, lambda_l2=lambda_l2,
                                                          precision=tolerance/np.finfo(float).eps,
                                                          num_lbfgs_corrections=num_of_curvature_pairs,
                                                          max_iter=num_iterations)

    def __call__(self, training_job_queue: Queue):
        """
        Call method to read training jobs off of a shared queue
        :param training_job_queue:      Shared multiprocessing job queue
        :return: None
        """
        processed_counter = 0
        logger.info(f"Kicking off training job consumer with ID : {self.consumer_id}")
        for training_job in iter(lambda: training_job_queue.get(True, self.timeout_in_seconds), None):
            # Train model
            entity_id = training_job.entity_id

            old_training_result = self.training_results.get(entity_id, None)
            theta_initial = old_training_result.training_result if old_training_result else None
            training_result = self.lr_trainer.fit(X=training_job.X,
                                                  y=training_job.y,
                                                  weights=training_job.weights,
                                                  offsets=training_job.offsets,
                                                  theta_initial=theta_initial)
            # Map trained model to entity ID
            self.training_results[entity_id] = TrainingResult(training_result=training_result[0],
                                                              unique_global_indices=training_job.
                                                              unique_global_indices)

            processed_counter += 1
            if processed_counter % TrainingJobConsumer._CONSUMER_LOGGING_FREQUENCY == 0:
                logger.info(f"Consumer job {self.consumer_id} has completed {processed_counter} training jobs so far")
        # If producer is done producing jobs, terminate consumer
        logger.info(f"Terminating consumer {self.consumer_id}")
