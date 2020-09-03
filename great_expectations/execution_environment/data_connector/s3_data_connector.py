import datetime
import logging
import re
import warnings
from io import StringIO

from great_expectations.exceptions import (BatchKwargsError,
                                           GreatExpectationsError)
from great_expectations.execution_environment.data_connector.data_connector import \
    DataConnector
from great_expectations.execution_environment.types import S3BatchKwargs
from great_expectations.execution_environment.util import S3Url

try:
    import boto3
except ImportError:
    boto3 = None

logger = logging.getLogger(__name__)


class S3GlobReaderDataConnector(DataConnector):
    """
    S3 DataConnector provides support for generating batches of data from an S3 bucket. For the S3 data connector, assets must
    be individually defined using a prefix and glob, although several additional configuration parameters are available
    for assets (see below).

    Example configuration::

        execution_environments:
          my_execution_env:
            ...
            data_connector:
              my_s3_data_connector:
                class_name: S3GlobReaderDataConnector
                bucket: my_bucket.my_organization.priv
                reader_method: parquet  # This will be automatically inferred from suffix where possible, but can be explicitly specified as well
                reader_options:  # Note that reader options can be specified globally or per-asset
                  sep: ","
                delimiter: "/"  # Note that this is the delimiter for the BUCKET KEYS. By default it is "/"
                boto3_options:
                  endpoint_url: $S3_ENDPOINT  # Use the S3_ENDPOINT environment variable to determine which endpoint to use
                max_keys: 100  # The maximum number of keys to fetch in a single list_objects request to s3. When accessing batch_kwargs through an iterator, the iterator will silently refetch if more keys were available
                assets:
                  my_first_asset:
                    prefix: my_first_asset/
                    regex_filter: .*  # The regex filter will filter the results returned by S3 for the key and prefix to only those matching the regex
                    directory_assets: True # if True, the contents of the directory will be treated as one batch. Notice that this option does not work with Pandas, since Pandas does not support loading multiple files from and S3 bucket into a data frame.
                  access_logs:
                    prefix: access_logs
                    regex_filter: access_logs/2019.*\.csv.gz
                    sep: "~"
                    max_keys: 100
    """

    recognized_batch_definition_keys = {
        "execution_environment" "data_asset_name",
        "partition_id",
        "reader_method",
        "reader_options",
        "limit",
    }

    # FIXME add tests for new partitioner functionality
    def __init__(
        self,
        name="default",
        execution_environment=None,
        bucket=None,
        reader_options=None,
        assets=None,
        delimiter="/",
        reader_method=None,
        boto3_options=None,
        max_keys=1000,
    ):
        """Initialize a new S3GlobReaderDataConnector

        Args:
            name: the name of the data connector
            execution_environment: the execution_environment to which it is attached
            bucket: the name of the s3 bucket from which it generates batch_kwargs
            reader_options: options passed to the execution_environment reader method
            assets: asset configuration (see class docstring for more information)
            delimiter: the BUCKET KEY delimiter
            reader_method: the reader_method to include in generated batch_kwargs
            boto3_options: dictionary of key-value pairs to use when creating boto3 client or resource objects
            max_keys: the maximum number of keys to fetch in a single list_objects request to s3
        """
        super().__init__(name, execution_environment=execution_environment)
        if reader_options is None:
            reader_options = {}

        if assets is None:
            assets = {"default": {"prefix": "", "regex_filter": ".*"}}

        self._bucket = bucket
        self._reader_method = reader_method
        self._reader_options = reader_options
        self._assets = assets
        self._delimiter = delimiter
        if boto3_options is None:
            boto3_options = {}
        self._max_keys = max_keys
        self._iterators = {}
        try:
            self._s3 = boto3.client("s3", **boto3_options)
        except TypeError:
            raise (
                ImportError(
                    "Unable to load boto3, which is required for S3 data connector"
                )
            )

    @property
    def reader_options(self):
        return self._reader_options

    @property
    def reader_method(self):
        return self._reader_method

    @property
    def assets(self):
        return self._assets

    @property
    def bucket(self):
        return self._bucket

    def get_available_data_asset_names(self):
        return {"names": [(key, "file") for key in self._assets.keys()]}

    def _get_iterator(
        self,
        data_asset_name,
        reader_method=None,
        reader_options=None,
        limit=None,
        **kwargs
    ):
        logger.debug(
            "Beginning S3GlobReaderDataConnector _get_iterator for data_asset_name: %s"
            % data_asset_name
        )

        if data_asset_name not in self._assets:
            batch_kwargs = {
                "data_asset_name": data_asset_name,
                "reader_method": reader_method,
                "reader_options": reader_options,
                "limit": limit,
            }
            raise BatchKwargsError(
                "Unknown asset_name %s" % data_asset_name, batch_kwargs
            )

        if data_asset_name not in self._iterators:
            self._iterators[data_asset_name] = {}

        asset_config = self._assets[data_asset_name]

        return self._build_asset_iterator(
            asset_config=asset_config,
            iterator_dict=self._iterators[data_asset_name],
            reader_method=reader_method,
            reader_options=reader_options,
            limit=limit,
        )

    def _build_batch_kwargs_path_iter(self, path_list, reader_options=None, limit=None):
        for path in path_list:
            yield self._build_batch_kwargs(
                path, reader_options=reader_options, limit=limit
            )

    def _build_batch_kwargs(self, batch_parameters):
        try:
            data_asset_name = batch_parameters.pop("data_asset_name")
        except KeyError:
            raise BatchKwargsError(
                "Unable to build BatchKwargs: no name provided in batch_parameters.",
                batch_kwargs=batch_parameters,
            )

        partition_id = batch_parameters.pop("partition_id", None)
        batch_kwargs = self._execution_environment.execution_engine.process_batch_definition(
            reader_method=batch_parameters.get("reader_method") or self.reader_method,
            reader_options=batch_parameters.get("reader_options")
            or self.reader_options,
            limit=batch_parameters.get("limit"),
        )

        if partition_id:
            try:
                asset_config = self._assets[data_asset_name]
            except KeyError:
                raise GreatExpectationsError(
                    "No asset config found for asset %s" % data_asset_name
                )
            if data_asset_name not in self._iterators:
                self._iterators[data_asset_name] = {}

            iterator_dict = self._iterators[data_asset_name]
            for key in self._get_asset_options(asset_config, iterator_dict):
                if (
                    self._partitioner(key=key, asset_config=asset_config)
                    == partition_id
                ):
                    batch_kwargs = self._build_batch_kwargs_from_key(
                        key=key,
                        asset_config=asset_config,
                        reader_options=batch_parameters.get(
                            "reader_options"
                        ),  # handled in generator
                        limit=batch_kwargs.get(
                            "limit"
                        ),  # may have been processed from engine
                    )

            if batch_kwargs is None:
                raise BatchKwargsError(
                    "Unable to identify partition %s for asset %s"
                    % (partition_id, data_asset_name),
                    {data_asset_name: data_asset_name, partition_id: partition_id},
                )

            self.batch_kwargs = batch_kwargs
            return batch_kwargs

        else:
            return self.yield_batch_kwargs(
                data_asset_name=data_asset_name, **batch_parameters, **batch_kwargs
            )

    def _build_batch_kwargs_from_key(
        self,
        key,
        asset_config=None,
        reader_method=None,
        reader_options=None,
        limit=None,
    ):
        batch_kwargs = {
            "s3": "s3a://" + self.bucket + "/" + key,
            "reader_options": self.reader_options,
        }
        if asset_config.get("reader_options"):
            batch_kwargs["reader_options"].update(asset_config.get("reader_options"))
        if reader_options is not None:
            batch_kwargs["reader_options"].update(reader_options)

        if self._reader_method is not None:
            batch_kwargs["reader_method"] = self._reader_method
        if asset_config.get("reader_method"):
            batch_kwargs["reader_method"] = asset_config.get("reader_method")
        if reader_method is not None:
            batch_kwargs["reader_method"] = reader_method

        if limit:
            batch_kwargs["limit"] = limit

        return S3BatchKwargs(batch_kwargs)

    def _get_asset_options(self, asset_config, iterator_dict):
        query_options = {
            "Bucket": self.bucket,
            "Delimiter": asset_config.get("delimiter", self._delimiter),
            "Prefix": asset_config.get("prefix", None),
            "MaxKeys": asset_config.get("max_keys", self._max_keys),
        }
        directory_assets = asset_config.get("directory_assets", False)

        if "continuation_token" in iterator_dict:
            query_options.update(
                {"ContinuationToken": iterator_dict["continuation_token"]}
            )

        logger.debug(
            "Fetching objects from S3 with query options: %s" % str(query_options)
        )
        asset_options = self._s3.list_objects_v2(**query_options)
        if directory_assets:
            if "CommonPrefixes" not in asset_options:
                raise BatchKwargsError(
                    "Unable to build batch_kwargs. The asset may not be configured correctly. If directory assets "
                    "are requested, then common prefixes must be returned.",
                    {
                        "asset_configuration": asset_config,
                        "contents": asset_options["Contents"]
                        if "Contents" in asset_options
                        else None,
                    },
                )
            keys = [item["Prefix"] for item in asset_options["CommonPrefixes"]]
        else:
            if "Contents" not in asset_options:
                raise BatchKwargsError(
                    "Unable to build batch_kwargs. The asset may not be configured correctly. If s3 returned common "
                    "prefixes it may not have been able to identify desired keys, and they are included in the "
                    "incomplete batch_kwargs object returned with this error.",
                    {
                        "asset_configuration": asset_config,
                        "common_prefixes": asset_options["CommonPrefixes"]
                        if "CommonPrefixes" in asset_options
                        else None,
                    },
                )
            keys = [
                item["Key"] for item in asset_options["Contents"] if item["Size"] > 0
            ]

        keys = [
            key
            for key in filter(
                lambda x: re.match(asset_config.get("regex_filter", ".*"), x)
                is not None,
                keys,
            )
        ]
        for key in keys:
            yield key

        if asset_options["IsTruncated"]:
            iterator_dict["continuation_token"] = asset_options["NextContinuationToken"]
            # Recursively fetch more
            for key in self._get_asset_options(asset_config, iterator_dict):
                yield key
        elif "continuation_token" in iterator_dict:
            # Make sure we clear the token once we've gotten fully through
            del iterator_dict["continuation_token"]

    def _build_asset_iterator(
        self,
        asset_config,
        iterator_dict,
        reader_method=None,
        reader_options=None,
        limit=None,
    ):
        for key in self._get_asset_options(asset_config, iterator_dict):
            yield self._build_batch_kwargs_from_key(
                key,
                asset_config,
                reader_method=None,
                reader_options=reader_options,
                limit=limit,
            )

    # TODO: deprecate generator_asset argument
    def get_available_partition_ids(self, generator_asset=None, data_asset_name=None):
        assert (generator_asset and not data_asset_name) or (
            not generator_asset and data_asset_name
        ), "Please provide either generator_asset or data_asset_name."
        if generator_asset:
            warnings.warn(
                "The 'generator_asset' argument will be deprecated and renamed to 'data_asset_name'. "
                "Please update code accordingly.",
                DeprecationWarning,
            )
            data_asset_name = generator_asset

        if data_asset_name not in self._iterators:
            self._iterators[data_asset_name] = {}
        iterator_dict = self._iterators[data_asset_name]
        asset_config = self._assets[data_asset_name]
        available_ids = [
            self._partitioner(key=key, asset_config=asset_config)
            for key in self._get_asset_options(asset_config, iterator_dict)
        ]
        return available_ids

    def _partitioner(self, key, asset_config):
        if "partition_regex" in asset_config:
            match_group_id = asset_config.get("match_group_id", 1)
            matches = re.match(asset_config["partition_regex"], key)
            # In the case that there is a defined regex, the user *wanted* a partition. But it didn't match.
            # So, we'll add a *sortable* id
            if matches is None:
                logger.warning("No match found for key: %s" % key)
                return (
                    datetime.datetime.now(datetime.timezone.utc).strftime(
                        "%Y%m%dT%H%M%S.%fZ"
                    )
                    + "__unmatched"
                )
            else:
                try:
                    return matches.group(match_group_id)
                except IndexError:
                    logger.warning(
                        "No match group %d in key %s" % (match_group_id, key)
                    )
                    return (
                        datetime.datetime.now(datetime.timezone.utc).strftime(
                            "%Y%m%dT%H%M%S.%fZ"
                        )
                        + "__no_match_group"
                    )

        # If there is no partitioner defined, fall back on using the path as a partition_id
        else:
            prefix = asset_config.get("prefix", "")
            if key.startswith(prefix):
                key = key[len(prefix) :]
            return key

    def get_s3_object(self, batch_kwargs):
        s3 = self._s3
        raw_url = batch_kwargs["s3"]
        engine = self._execution_environment.execution_engine
        reader_method = batch_kwargs.get("reader_method")
        url = S3Url(raw_url)
        logger.debug("Fetching s3 object. Bucket: %s Key: %s" % (url.bucket, url.key))
        s3_object = s3.get_object(Bucket=url.bucket, Key=url.key)

        # try:
        #     import boto3
        #
        #     s3 = boto3.client("s3", **self._boto3_options)
        # except ImportError:
        #     raise BatchSpecError(
        #         "Unable to load boto3 client to read s3 asset.", batch_spec
        #     )
        # raw_url = batch_spec["s3"]
        # reader_method = batch_spec.get("reader_method")
        # url = S3Url(raw_url)
        # logger.debug(
        #     "Fetching s3 object. Bucket: %s Key: %s" % (url.bucket, url.key)
        # )
        # s3_object = s3.get_object(Bucket=url.bucket, Key=url.key)
        # reader_fn = self._get_reader_fn(reader_method, url.key)
        # df = reader_fn(
        #     StringIO(
        #         s3_object["Body"]
        #             .read()
        #             .decode(s3_object.get("ContentEncoding", "utf-8"))
        #     ),
        #     **reader_options
        # )

        return url, s3_object
